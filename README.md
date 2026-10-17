# MCP-ARD-XY: gateway MCP for testing ARD + deciders

A FastMCP gateway that imitates a Databricks-style setup: several Genie-like data agents, downstream
MCP proxies, a public API tool and agentic document search, behind one endpoint with an inbound
auth / rate-limit / audit layer. Built to test **agentic resource discovery (ARD)** and a **decider**
that must choose between overlapping tools using `routing_instructions`.

## What is exposed

| Tool(s) | Kind | Notes |
|---|---|---|
| `genie_sales_orders`, `genie_customer_support`, `genie_hr_people`, `genie_finance_ledger` | Genie stand-ins | Claude text-to-SQL over a read-only SQLite DB each. Domains overlap on purpose (customers, "revenue"). |
| `docs_agentic_search` | Agentic search | BM25 + Claude loop over `data/docs/*.md`. |
| `fx_latest`, `fx_history` | Public API | Frankfurter (no key). |
| `fetch_*`, `time_*` | Downstream MCP proxies | `mcp-server-fetch`, `mcp-server-time` via `uvx`. |
| `search_tools`, `describe_tool`, `call_tool` | Discovery meta-tools | Only visible in `meta` mode. |

Every tool carries tags and `routing_instructions` (see `catalog.py` / `spaces.py`).

## Discovery modes (`--discovery` or `GATEWAY_DISCOVERY`)

* `flat`: all tools listed (descriptions include routing instructions). The decider picks from the full list.
* `meta`: only `search_tools` -> `describe_tool` -> `call_tool`. Tests ARD proper. `catalog://tools` is also a resource.

## Run

```bash
uv sync
cp .env.example .env            # add ANTHROPIC_API_KEY (needed by genie_* and docs_agentic_search)
uv run mcp-gateway serve                                   # stdio
uv run mcp-gateway init-clients                            # writes clients.json with generated keys
uv run mcp-gateway serve --transport http --discovery meta # http://127.0.0.1:8000/mcp, bearer auth
```

Claude Desktop / Code (stdio): command `uv`, args `["--directory", "<this folder>", "run", "mcp-gateway", "serve"]`.

## Inbound layer (HTTP)

`security.py`: bearer API keys (`clients.json`), per-client sliding-window rate limit (429 + `Retry-After`),
per-client tool allow-lists (glob patterns; hidden from `tools/list` and blocked on call). stdio is trusted.

## Evaluating a decider

* `logs/audit.jsonl`: every tool call (client, session, tool, args, latency, `via_meta`, result preview).
* `logs/discovery.jsonl`: each `search_tools` / `describe_tool` with ranked results, joined to calls by `session`.

## Resources and Prompts

MCP has three primitives, split by **who decides to use them**:

| Primitive | Controlled by | Here |
|---|---|---|
| Tools | the model | `genie_*`, `docs_agentic_search`, `fx_*`, proxies, discovery meta-tools |
| Resources | the application / user (attached as context) | `routing://global`, `routing://tool/{name}`, `spaces://{key}/schema`, `catalog://tools` |
| Prompts | the user (picked like slash commands) | `route_question`, `analyze_space`, `investigate_across_spaces` |

The global routing policy lives in one file, [data/routing/global.md](data/routing/global.md), and reaches clients three ways:
as the `routing://global` resource, inside the server `instructions` (most clients put these in the system prompt), and
inside the `route_question` prompt. Tool-only clients still see per-tool routing text in each tool description.

## Evals

```bash
uv run pytest                                   # unit tests + evals (live ones auto-skip without a key)
uv run pytest -m live                           # only the LLM evals
uv run python scripts/eval_routing.py [--llm] [--trajectory]   # human-readable report -> eval/results/
uv run python scripts/http_smoke.py 8000        # inbound layer against a running HTTP server
```

* **Routing** ([eval/routing_cases.jsonl](eval/routing_cases.jsonl)): `search_tools` then a decider. Thresholds:
  baseline accuracy >= 0.90, recall@k = 1.0, LLM accuracy >= 0.95 (`EVAL_*` env vars).
* **Trajectory** ([eval/trajectories.jsonl](eval/trajectories.jsonl)): discover, decide and invoke, then replay
  `audit.jsonl` and assert the executed sequence, forbidden tools, call budget and errors.
* The LLM decider uses the server's `route_question` prompt, so the eval tests the prompt the server actually serves.
  A decider is any `async (client, settings, question, candidates) -> tool name` in `evals.py`: plug Jev, Laya or strands-decider in there.

## Notes

* Proxies use `mode="legacy"`: FastMCP 4.1 defaults to a newer handshake that older servers reject.
* Adding a space: append a `Space` in `spaces.py` with a DDL and seeder.
