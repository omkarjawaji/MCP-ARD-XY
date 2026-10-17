"""Tool catalog for agentic resource discovery (ARD).

Builds one `ToolCard` per tool the gateway can serve (native, Genie-like, proxied),
indexes cards with BM25 and answers `search_tools` queries. Cards carry the
`routing_instructions` a decider can weigh when several tools look similar.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from fastmcp.tools import Tool
from rank_bm25 import BM25Okapi

# Metadata for tools we do not author (downstream proxies), matched by name prefix.
PROXY_OVERLAY: dict[str, dict[str, Any]] = {
    "fetch_": {
        "source": "proxy:fetch",
        "tags": ["web", "fetch", "scrape", "markdown"],
        "routing_instructions": (
            "USE to read the live content of a specific URL. DO NOT USE for questions answerable "
            "from internal data (use genie_* spaces) or internal documents (use docs_agentic_search)."
        ),
    },
    "time_": {
        "source": "proxy:time",
        "tags": ["time", "timezone", "clock"],
        "routing_instructions": "USE for current time or timezone conversion only.",
    },
}

_TOKEN = re.compile(r"[a-z0-9]+")


def _tok(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("_", " "))


@dataclass
class ToolCard:
    name: str
    description: str
    source: str = "native"
    tags: list[str] = field(default_factory=list)
    routing_instructions: str = ""
    sample_questions: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)

    def brief(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "description": self.description.split("\n\n")[0],
            "tags": self.tags,
            "routing_instructions": self.routing_instructions,
        }

    def text(self) -> str:
        return " ".join([self.name, self.description, " ".join(self.tags),
                         self.routing_instructions, " ".join(self.sample_questions)])


def card_from_tool(tool: Tool) -> ToolCard:
    meta = (tool.meta or {})
    overlay = next((v for p, v in PROXY_OVERLAY.items() if tool.name.startswith(p)), {})
    return ToolCard(
        name=tool.name,
        description=tool.description or "",
        source=meta.get("source", overlay.get("source", "native")),
        tags=sorted(set(tool.tags or set()) | set(overlay.get("tags", []))),
        routing_instructions=meta.get("routing_instructions", overlay.get("routing_instructions", "")),
        sample_questions=list(meta.get("sample_questions", [])),
        input_schema=tool.parameters or {},
    )


class Catalog:
    def __init__(self, tools: Sequence[Tool], hidden: set[str]):
        self.cards = {c.name: c for c in map(card_from_tool, tools) if c.name not in hidden}
        self._names = list(self.cards)
        corpus = [_tok(self.cards[n].text()) for n in self._names]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, k: int = 5, tags: list[str] | None = None) -> list[tuple[ToolCard, float]]:
        if not self._bm25:
            return []
        scores = self._bm25.get_scores(_tok(query))
        ranked = sorted(zip(self._names, scores), key=lambda x: x[1], reverse=True)
        out = []
        for name, score in ranked:
            card = self.cards[name]
            if tags and not set(tags) & set(card.tags):
                continue
            if score > 0:
                out.append((card, float(score)))
            if len(out) >= k:
                break
        return out

    def describe(self, name: str) -> dict[str, Any] | None:
        card = self.cards.get(name)
        return asdict(card) if card else None
