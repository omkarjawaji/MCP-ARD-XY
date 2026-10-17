"""Evals as automated tests with CI-style thresholds.

Offline tests need no API key. Tests marked `live` call Claude and skip without ANTHROPIC_API_KEY:

    uv run pytest                 # offline evals + unit tests
    uv run pytest -m live         # LLM decider + trajectory scenarios that call Genie agents

Thresholds can be tightened over time through env vars.
"""

import os

import pytest
from fastmcp import Client

from mcp_gateway.config import load_settings
from mcp_gateway.evals import (CASES, SCENARIOS, llm_prompt_decider, load_jsonl, run_routing, run_trajectory,
                               top1_decider)
from mcp_gateway.gateway import build_server

BASELINE_MIN_ACC = float(os.getenv("EVAL_BASELINE_MIN_ACC", "0.90"))
LLM_MIN_ACC = float(os.getenv("EVAL_LLM_MIN_ACC", "0.95"))
MIN_RECALL = float(os.getenv("EVAL_MIN_RECALL_AT_K", "1.0"))

needs_key = pytest.mark.skipif(not load_settings().anthropic_api_key, reason="ANTHROPIC_API_KEY not set")


@pytest.fixture
async def gw(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_LOG_DIR", str(tmp_path / "logs"))  # isolate audit log per test
    settings = load_settings("meta")
    async with Client(build_server(settings)) as c:
        yield c, settings


async def test_routing_baseline_threshold(gw):
    c, settings = gw
    rep = await run_routing(c, settings, load_jsonl(CASES), top1_decider)
    misses = [(r["q"], r["chosen"]) for r in rep["rows"] if not r["correct"]]
    assert rep["recall_at_k"] >= MIN_RECALL, f"discovery lost the right tool: {rep['recall_at_k']}"
    assert rep["accuracy"] >= BASELINE_MIN_ACC, f"accuracy {rep['accuracy']:.2f}; misses: {misses}"


async def test_trajectory_offline_scenarios(gw):
    c, settings = gw
    for s in (s for s in load_jsonl(SCENARIOS) if not s.get("live")):
        r = await run_trajectory(c, settings, s, top1_decider)
        assert r.passed, f"{s['id']}: {r.problems}"


async def test_prompt_and_resources_are_served(gw):
    c, _ = gw
    uris = {str(r.uri) for r in await c.list_resources()}
    assert {"routing://global", "catalog://tools"} <= uris
    templates = {t.uri_template for t in await c.list_resource_templates()}
    assert {"routing://tool/{name}", "spaces://{key}/schema"} <= templates
    assert "Global routing policy" in (await c.get_prompt("route_question", {"question": "overdue invoices"})).messages[0].content.text
    schema = (await c.read_resource("spaces://hr_people/schema"))[0].text
    assert "CREATE TABLE employees" in schema
    assert {"route_question", "analyze_space", "investigate_across_spaces"} <= {p.name for p in await c.list_prompts()}


@pytest.mark.live
@needs_key
async def test_routing_llm_threshold(gw):
    c, settings = gw
    rep = await run_routing(c, settings, load_jsonl(CASES), llm_prompt_decider)
    misses = [(r["q"], r["chosen"]) for r in rep["rows"] if not r["correct"]]
    assert rep["accuracy"] >= LLM_MIN_ACC, f"accuracy {rep['accuracy']:.2f}; misses: {misses}"


@pytest.mark.live
@needs_key
async def test_trajectory_live_scenarios(gw):
    c, settings = gw
    for s in load_jsonl(SCENARIOS):
        r = await run_trajectory(c, settings, s, llm_prompt_decider)
        assert r.passed, f"{s['id']}: {r.problems}"
