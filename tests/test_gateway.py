"""Smoke tests that need no network and no LLM key (proxies are spawned via uvx)."""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_gateway.agents import validate_select
from mcp_gateway.config import load_settings
from mcp_gateway.gateway import META_TOOLS, build_server
from mcp_gateway.security import Client as GwClient, SlidingWindow, current_client


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GATEWAY_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "data" / "docs").mkdir(parents=True)
    (tmp_path / "data" / "docs" / "a.md").write_text("Refunds within 30 days.\n\nEscalate sev1 fast.")
    return lambda mode: load_settings(mode)


async def names(server):
    async with Client(server) as c:
        return {t.name for t in await c.list_tools()}


async def test_flat_lists_everything_but_meta(settings):
    got = await names(build_server(settings("flat")))
    assert not got & META_TOOLS
    assert {"genie_sales_orders", "genie_finance_ledger", "docs_agentic_search", "fx_latest"} <= got
    assert {"fetch_fetch", "time_get_current_time"} <= got


async def test_meta_lists_only_discovery_tools(settings):
    assert await names(build_server(settings("meta"))) == META_TOOLS


async def test_search_routes_to_expected_space(settings):
    async with Client(build_server(settings("meta"))) as c:
        r = await c.call_tool("search_tools", {"query": "which customers have open sev1 tickets and low health", "k": 3})
        top = [x["name"] for x in r.data["results"]]
        assert top[0] == "genie_customer_support"
        r = await c.call_tool("search_tools", {"query": "overdue invoices budget variance"})
        assert r.data["results"][0]["name"] == "genie_finance_ledger"
        d = await c.call_tool("describe_tool", {"name": "genie_hr_people"})
        assert "routing_instructions" in d.data


async def test_allowlist_blocks_and_hides(settings):
    server = build_server(settings("flat"))
    tok = current_client.set(GwClient(id="t", allow_tools=("fx_*",)))
    try:
        async with Client(server) as c:
            assert {t.name for t in await c.list_tools()} == {"fx_latest", "fx_history"}
            with pytest.raises(ToolError):
                await c.call_tool("genie_hr_people", {"question": "headcount"})
    finally:
        current_client.reset(tok)


async def test_audit_log_written(settings, tmp_path):
    async with Client(build_server(settings("meta"))) as c:
        await c.call_tool("search_tools", {"query": "time zone"})
        await c.call_tool("call_tool", {"name": "time_get_current_time", "arguments": {"timezone": "UTC"}})
    audit = [json.loads(l) for l in (tmp_path / "logs" / "audit.jsonl").read_text().splitlines()]
    assert any(a["tool"] == "time_get_current_time" and a["via_meta"] for a in audit)
    assert (tmp_path / "logs" / "discovery.jsonl").exists()


def test_sql_validation():
    assert validate_select("SELECT 1;") == "SELECT 1"
    for bad in ["DROP TABLE x", "select 1; select 2", "with a as (select 1) delete from t", "PRAGMA table_info(x)"]:
        with pytest.raises(ValueError):
            validate_select(bad)


def test_sliding_window():
    w = SlidingWindow()
    assert all(w.check("k", 3) == 0 for _ in range(3))
    assert w.check("k", 3) > 0
