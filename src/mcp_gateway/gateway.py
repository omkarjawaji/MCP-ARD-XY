"""Server assembly: native tools, downstream proxies, discovery meta-tools and middleware."""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Annotated, Any

import httpx
from fastmcp import Context, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .agents import DocSearchAgent, GenieAgent
from .catalog import Catalog
from .config import Settings
from .logs import JsonlLog, preview
from .security import current_client
from .spaces import SPACES, SPACES_BY_KEY

META_TOOLS = {"search_tools", "describe_tool", "call_tool"}
via_meta: ContextVar[bool] = ContextVar("via_meta", default=False)

FRANKFURTER = "https://api.frankfurter.dev/v1"

# Downstream MCP servers, each proxied under its own namespace (-> fetch_*, time_*).
# `mode="legacy"` forces the classic initialize handshake that these servers speak.
DOWNSTREAMS: dict[str, tuple[str, list[str]]] = {
    "fetch": ("uvx", ["mcp-server-fetch"]),
    "time": ("uvx", ["mcp-server-time"]),
}


def _session_id(ctx: Context | None) -> str | None:
    try:
        return ctx.session_id if ctx else None
    except Exception:
        return None


class GatewayMiddleware(Middleware):
    """Per-client tool visibility, authorisation and audit logging."""

    def __init__(self, settings: Settings, audit: JsonlLog):
        self.settings = settings
        self.audit = audit

    def _visible(self, name: str) -> bool:
        if not current_client.get().allows(name):
            return False
        is_meta = name in META_TOOLS
        return is_meta if self.settings.discovery_mode == "meta" else not is_meta

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        return [t for t in tools if self._visible(t.name)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        client = current_client.get()
        name = context.message.name
        args = context.message.arguments or {}
        base = dict(client=client.id, session=_session_id(context.fastmcp_context), tool=name,
                    args=preview(args), via_meta=via_meta.get(), mode=self.settings.discovery_mode)
        if not client.allows(name):
            self.audit.write(**base, ok=False, error="forbidden")
            raise ToolError(f"Client '{client.id}' is not permitted to call '{name}'.")
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            self.audit.write(**base, ok=False, error=f"{type(exc).__name__}: {exc}"[:300],
                             ms=round((time.perf_counter() - start) * 1000))
            raise
        self.audit.write(**base, ok=True, ms=round((time.perf_counter() - start) * 1000),
                         result=preview(result.structured_content or [getattr(c, "text", "") for c in result.content]))
        return result


def _make_genie_fn(agent: GenieAgent):
    async def ask(question: Annotated[str, "A natural-language question about this space's data"]) -> dict:
        return await agent.ask(question)

    return ask


def _add_native_tools(mcp: FastMCP, settings: Settings) -> None:
    for space in SPACES:
        mcp.tool(
            _make_genie_fn(GenieAgent(space, settings)),
            name=f"genie_{space.key}",
            description=f"{space.description}\n\nRouting instructions: {space.routing_instructions}",
            tags=set(space.tags) | {"genie", "sql"},
            meta={"source": "genie", "routing_instructions": space.routing_instructions,
                  "sample_questions": space.sample_questions},
            annotations={"readOnlyHint": True, "openWorldHint": False},
        )

    docs = DocSearchAgent(settings)

    @mcp.tool(
        name="docs_agentic_search",
        description=(
            "Multi-step search over unstructured internal documents (policies, runbooks, "
            "product notes, meeting minutes). Returns a cited answer.\n\nRouting instructions: "
            "USE for policy, process, how-we-work or narrative questions. DO NOT USE for numeric "
            "questions answerable from tables (use genie_* spaces)."
        ),
        tags={"docs", "search", "unstructured", "rag"},
        meta={"source": "agent", "routing_instructions": "USE for policy/process/narrative questions in documents; NOT for numbers in tables.",
              "sample_questions": ["What is our refund policy?", "How do we escalate a sev1 incident?"]},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    async def docs_agentic_search(question: Annotated[str, "Question to answer from the document collection"]) -> dict:
        return await docs.ask(question)

    async def _frankfurter(path: str, params: dict[str, str]) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                r = await http.get(f"{FRANKFURTER}/{path}", params=params)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPError as exc:
            raise ToolError(f"Frankfurter API request failed: {exc}") from exc

    fx_meta = {"source": "api:frankfurter",
               "routing_instructions": "USE for live or historical public FX rates. For FX rates stored in the finance ledger "
                                       "alongside invoices, use genie_finance_ledger."}

    @mcp.tool(name="fx_latest", tags={"fx", "currency", "api", "finance"}, meta=fx_meta,
              description="Latest ECB foreign-exchange rates from the Frankfurter public API.",
              annotations={"readOnlyHint": True, "openWorldHint": True})
    async def fx_latest(base: str = "USD", symbols: Annotated[str, "Comma-separated, e.g. EUR,GBP"] = "") -> dict:
        return await _frankfurter("latest", {"base": base.upper(), **({"symbols": symbols.upper()} if symbols else {})})

    @mcp.tool(name="fx_history", tags={"fx", "currency", "api", "finance"}, meta=fx_meta,
              description="Historical FX rates between two dates (YYYY-MM-DD) from the Frankfurter public API.",
              annotations={"readOnlyHint": True, "openWorldHint": True})
    async def fx_history(start: str, end: str, base: str = "USD",
                         symbols: Annotated[str, "Comma-separated, e.g. EUR,GBP"] = "") -> dict:
        return await _frankfurter(f"{start}..{end}",
                                  {"base": base.upper(), **({"symbols": symbols.upper()} if symbols else {})})


def _add_downstream_proxies(mcp: FastMCP) -> None:
    for ns, (cmd, args) in DOWNSTREAMS.items():
        mcp.mount(create_proxy(StdioTransport(cmd, args), mode="legacy"), namespace=ns)


def _add_discovery_tools(mcp: FastMCP, settings: Settings, disc_log: JsonlLog) -> None:
    async def _catalog(ctx: Context) -> Catalog:
        tools = await ctx.fastmcp.list_tools(run_middleware=False)
        return Catalog(tools, hidden=META_TOOLS)

    def _visible(card_name: str) -> bool:
        return current_client.get().allows(card_name)

    @mcp.tool(annotations={"readOnlyHint": True},
              description="Search the gateway's tool catalog by natural language. Returns ranked tool cards "
                          "with descriptions and routing instructions. Use describe_tool for the input schema.")
    async def search_tools(ctx: Context, query: str, k: int = 5, tags: list[str] | None = None) -> dict:
        cat = await _catalog(ctx)
        hits = [(c, s) for c, s in cat.search(query, k=k * 3, tags=tags) if _visible(c.name)][:k]
        disc_log.write(event="search_tools", client=current_client.get().id, session=_session_id(ctx),
                       query=query, tags=tags, results=[{"tool": c.name, "score": round(s, 3)} for c, s in hits])
        return {"query": query, "results": [{**c.brief(), "score": round(s, 3)} for c, s in hits]}

    @mcp.tool(annotations={"readOnlyHint": True},
              description="Full card for one tool: description, routing instructions, sample questions, input schema.")
    async def describe_tool(ctx: Context, name: str) -> dict:
        card = (await _catalog(ctx)).describe(name) if _visible(name) else None
        disc_log.write(event="describe_tool", client=current_client.get().id, session=_session_id(ctx),
                       tool=name, found=card is not None)
        if card is None:
            raise ToolError(f"Unknown tool '{name}'. Use search_tools to find tools.")
        return card

    @mcp.tool(description="Invoke any catalog tool by name with a JSON arguments object.")
    async def call_tool(ctx: Context, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name in META_TOOLS:
            raise ToolError("call_tool cannot invoke discovery tools.")
        tok = via_meta.set(True)
        try:
            result = await ctx.fastmcp.call_tool(name, arguments or {})
        finally:
            via_meta.reset(tok)
        if result.structured_content is not None:
            return result.structured_content
        return "\n".join(getattr(c, "text", str(c)) for c in result.content)

    @mcp.resource("catalog://tools", mime_type="application/json",
                  description="Machine-readable catalog of every tool card the gateway can serve.")
    async def catalog_resource(ctx: Context) -> list[dict]:
        cat = await _catalog(ctx)
        return [c.brief() for c in cat.cards.values() if _visible(c.name)]


def _read_routing(settings: Settings) -> str:
    try:
        return settings.routing_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _add_resources(mcp: FastMCP, settings: Settings) -> None:
    """Resources: read-only context the *client/user* chooses to attach (application-controlled)."""

    @mcp.resource("routing://global", mime_type="text/markdown",
                  description="Shared routing policy applied before any single tool's own routing instructions.")
    def routing_global() -> str:
        return _read_routing(settings)

    @mcp.resource("routing://tool/{name}", mime_type="text/markdown",
                  description="Routing instructions and sample questions for one tool.")
    async def routing_for_tool(name: str, ctx: Context) -> str:
        cat = Catalog(await ctx.fastmcp.list_tools(run_middleware=False), META_TOOLS)
        card = cat.describe(name)
        if card is None or not current_client.get().allows(name):
            raise ToolError(f"Unknown tool '{name}'.")
        samples = "\n".join(f"- {q}" for q in card["sample_questions"]) or "- (none)"
        return f"# {name}\n\n{card['routing_instructions'] or '(no routing instructions)'}\n\nSample questions:\n{samples}\n"

    @mcp.resource("spaces://{key}/schema", mime_type="text/plain",
                  description="SQL schema (DDL) of one Genie-like data space.")
    def space_schema(key: str) -> str:
        space = SPACES_BY_KEY.get(key)
        if space is None:
            raise ToolError(f"Unknown space '{key}'. Known: {sorted(SPACES_BY_KEY)}")
        return f"-- {space.title}\n{space.ddl.strip()}\n"


def _add_prompts(mcp: FastMCP, settings: Settings) -> None:
    """Prompts: reusable templates the *user* picks (user-controlled), like slash commands."""

    @mcp.prompt(description="Decider prompt: pick the single best tool for a question from the top-k search candidates.")
    async def route_question(question: str, ctx: Context, k: int = 4) -> str:
        cat = Catalog(await ctx.fastmcp.list_tools(run_middleware=False), META_TOOLS)
        hits = [c for c, _ in cat.search(question, k=k * 3) if current_client.get().allows(c.name)][:k]
        menu = "\n\n".join(f"- {c.name}: {c.brief()['description']}\n  routing: {c.routing_instructions}" for c in hits)
        return (
            f"Global routing policy:\n{_read_routing(settings)}\n\n"
            f"Question: {question}\n\nCandidate tools:\n{menu or '(none)'}\n\n"
            "Reply with only the single best tool name, or NONE."
        )

    @mcp.prompt(description="Guided analysis of one data space: explore the schema, then answer a goal with genie_* calls.")
    def analyze_space(space: str, goal: str) -> str:
        if space not in SPACES_BY_KEY:
            raise ToolError(f"Unknown space '{space}'. Known: {sorted(SPACES_BY_KEY)}")
        return (
            f"Analyze the '{space}' space to achieve this goal: {goal}\n\n"
            f"1. Read the resource spaces://{space}/schema.\n"
            f"2. Break the goal into 2-4 concrete questions.\n"
            f"3. Ask each one with the genie_{space} tool.\n"
            "4. Summarize findings with the exact numbers returned and note any caveats."
        )

    @mcp.prompt(description="Investigate a question that spans several spaces, deciding which space owns each part.")
    def investigate_across_spaces(question: str) -> str:
        return (
            f"Investigate: {question}\n\nFirst read routing://global. Split the question into parts, "
            "assign each part to exactly one tool using the routing policy, call the tools, and combine "
            "the results. State which tool answered which part and flag any metric definitions that differ "
            "between spaces (for example 'revenue')."
        )


def build_server(settings: Settings) -> FastMCP:
    mcp = FastMCP(
        "mcp-gateway",
        instructions=(
            "Gateway over several Genie-like data spaces, agentic document search, a public FX API "
            "and downstream MCP servers (fetch, time). In 'meta' discovery mode, call search_tools "
            "first, then describe_tool, then call_tool. Resources: routing://global, "
            "routing://tool/{name}, spaces://{key}/schema, catalog://tools. Routing policy:\n\n"
            + _read_routing(settings)
        ),
    )
    audit = JsonlLog(settings.log_dir / "audit.jsonl")
    disc = JsonlLog(settings.log_dir / "discovery.jsonl")
    _add_native_tools(mcp, settings)
    _add_downstream_proxies(mcp)
    _add_discovery_tools(mcp, settings, disc)
    _add_resources(mcp, settings)
    _add_prompts(mcp, settings)
    mcp.add_middleware(GatewayMiddleware(settings, audit))
    return mcp
