"""LLM-driven agents behind the gateway's native tools.

* `GenieAgent`  - text-to-SQL over one read-only SQLite space (Genie stand-in).
* `DocSearchAgent` - multi-step retrieval over a folder of documents.

Both run a small Anthropic tool-use loop; see `_run_loop`.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic
from fastmcp.exceptions import ToolError
from rank_bm25 import BM25Okapi

from .config import Settings
from .spaces import Space, ensure_space_db

ToolImpl = Callable[[dict[str, Any]], Awaitable[Any]]


def _client(settings: Settings) -> AsyncAnthropic:
    if not settings.anthropic_api_key:
        raise ToolError("ANTHROPIC_API_KEY is not set; this tool needs an LLM to plan its steps.")
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _run_loop(
    settings: Settings,
    system: str,
    question: str,
    tools: list[dict],
    impls: dict[str, ToolImpl],
    trace: list[dict],
) -> str:
    """Run tool-use turns until the model answers in plain text or steps run out."""
    client = _client(settings)
    messages: list[dict] = [{"role": "user", "content": question}]
    for _ in range(settings.max_agent_steps):
        resp = await client.messages.create(
            model=settings.agent_model, max_tokens=1500, system=system, tools=tools, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                out = await impls[block.name](block.input)
                content, is_error = str(out), False
            except Exception as exc:  # surfaced to the model so it can retry
                content, is_error = f"Error: {exc}", True
            trace.append({"tool": block.name, "input": block.input, "error": is_error})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "Stopped: step limit reached before a final answer."


# --------------------------------------------------------------------------- SQL

_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum)\b", re.I)


def validate_select(sql: str) -> str:
    """Allow exactly one SELECT/WITH statement. Defence in depth; the DB is also opened read-only."""
    stmt = sql.strip().rstrip(";").strip()
    if ";" in stmt:
        raise ValueError("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", stmt, re.I):
        raise ValueError("Only SELECT queries are allowed.")
    if _FORBIDDEN.search(stmt):
        raise ValueError("Query contains a forbidden keyword.")
    return stmt


def _query(db: Path, sql: str, limit: int) -> tuple[list[str], list[tuple], bool]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        budget = [2_000_000]

        def _abort() -> int:  # stop runaway queries
            budget[0] -= 1
            return 1 if budget[0] <= 0 else 0

        conn.set_progress_handler(_abort, 1000)
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
        return cols, rows[:limit], len(rows) > limit
    finally:
        conn.close()


class GenieAgent:
    def __init__(self, space: Space, settings: Settings):
        self.space = space
        self.settings = settings
        self.db = ensure_space_db(space, settings.data_dir)

    def _schema(self) -> str:
        return self.space.ddl.strip()

    async def ask(self, question: str) -> dict:
        trace: list[dict] = []
        last: dict[str, Any] = {}

        async def run_sql(args: dict) -> str:
            sql = validate_select(args["sql"])
            cols, rows, truncated = await asyncio.to_thread(_query, self.db, sql, self.settings.sql_row_limit)
            last.update(sql=sql, columns=cols, rows=[list(r) for r in rows], truncated=truncated)
            body = "\n".join(" | ".join(map(str, r)) for r in rows) or "(no rows)"
            return f"{' | '.join(cols)}\n{body}" + ("\n(truncated)" if truncated else "")

        system = (
            f"You are a data analyst agent for the '{self.space.title}' space. "
            f"{self.space.description}\nSQLite schema:\n{self._schema()}\n"
            "Write SQLite SQL and call run_sql. Use only these tables. "
            "If the question cannot be answered from this schema, say so plainly and name what is missing "
            "instead of guessing. Finish with a short answer that states the numbers."
        )
        tools = [{
            "name": "run_sql",
            "description": "Run one read-only SQLite SELECT query and return rows.",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        }]
        answer = await _run_loop(self.settings, system, question, tools, {"run_sql": run_sql}, trace)
        return {"space": self.space.key, "answer": answer, **last, "steps": len(trace)}


# ------------------------------------------------------------------- doc search

_TOKEN = re.compile(r"[a-z0-9]+")


def _tok(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class DocIndex:
    def __init__(self, docs_dir: Path):
        self.docs: dict[str, str] = {}
        self.chunks: list[tuple[str, int, str]] = []  # (doc, chunk_no, text)
        for path in sorted(docs_dir.glob("**/*")):
            if path.suffix.lower() in {".md", ".txt"} and path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                name = path.relative_to(docs_dir).as_posix()
                self.docs[name] = text
                for i, part in enumerate(p for p in re.split(r"\n\s*\n", text) if p.strip()):
                    self.chunks.append((name, i, part.strip()))
        self.bm25 = BM25Okapi([_tok(c[2]) for c in self.chunks]) if self.chunks else None

    def search(self, query: str, k: int = 5) -> list[tuple[str, int, str, float]]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(_tok(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        return [(*self.chunks[i], float(scores[i])) for i in order if scores[i] > 0]


class DocSearchAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.index = DocIndex(settings.docs_dir)

    async def ask(self, question: str) -> dict:
        trace: list[dict] = []
        cited: set[str] = set()

        async def search_docs(args: dict) -> str:
            hits = self.index.search(args["query"], int(args.get("k", 5)))
            for name, _, _, _ in hits:
                cited.add(name)
            return "\n\n".join(f"[{n}#{i}] (score {s:.2f})\n{t}" for n, i, t, s in hits) or "No matches."

        async def read_doc(args: dict) -> str:
            name = args["doc"]
            if name not in self.index.docs:
                raise ValueError(f"Unknown doc {name!r}. Known: {sorted(self.index.docs)}")
            cited.add(name)
            return self.index.docs[name][:6000]

        system = (
            "You answer questions from an internal document collection. Search with several "
            "differently-worded queries, read full documents when a snippet looks relevant, and "
            "answer only from what you found. Cite sources as [doc#chunk]. If the documents do not "
            "contain the answer, say so."
        )
        tools = [
            {"name": "search_docs", "description": "BM25 keyword search over document chunks.",
             "input_schema": {"type": "object", "properties": {"query": {"type": "string"},
                              "k": {"type": "integer"}}, "required": ["query"]}},
            {"name": "read_doc", "description": "Read one whole document by file name.",
             "input_schema": {"type": "object", "properties": {"doc": {"type": "string"}},
                              "required": ["doc"]}},
        ]
        answer = await _run_loop(self.settings, system, question, tools,
                                 {"search_docs": search_docs, "read_doc": read_doc}, trace)
        return {"answer": answer, "sources": sorted(cited), "steps": len(trace)}
