"""Manual check of the inbound layer against a running HTTP gateway.

    uv run mcp-gateway serve --transport http --discovery meta --port 8765
    uv run python scripts/http_smoke.py 8765
"""

import asyncio
import json
import sys

import httpx
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

from mcp_gateway.config import load_settings
from mcp_gateway.security import load_clients

PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
URL = f"http://127.0.0.1:{PORT}/mcp"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "smoke", "version": "1"}}}
HDRS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def status(key: str | None) -> int:
    h = dict(HDRS, **({"Authorization": f"Bearer {key}"} if key else {}))
    async with httpx.AsyncClient() as http, http.stream("POST", URL, json=INIT, headers=h) as r:
        return r.status_code


async def main() -> None:
    clients = {c.id: c for c in load_clients(load_settings().clients_file)}
    print("no key      ->", await status(None))
    print("wrong key   ->", await status("wrong"))
    print("valid key   ->", await status(clients["admin"].api_key))

    async with Client(URL, auth=BearerAuth(clients["decider-eval"].api_key)) as c:
        print("decider-eval tools:", sorted(t.name for t in await c.list_tools()))
        r = await c.call_tool("search_tools", {"query": "overdue invoices and budget variance"})
        print("search top hit  :", r.data["results"][0]["name"] if r.data["results"] else None)

    limited = clients["readonly-web"]
    codes = [await status(limited.api_key) for _ in range(limited.rate_limit_per_min + 3)]
    print(f"rate limit ({limited.rate_limit_per_min}/min) ->", json.dumps(codes[-5:]))


asyncio.run(main())
