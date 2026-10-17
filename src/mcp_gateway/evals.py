"""Eval library shared by `scripts/eval_routing.py` and the pytest suite.

Two eval kinds:

* routing     - given a question, did discovery + a decider pick the right tool?
* trajectory  - after running a scenario end to end, does the *sequence of tools the
                gateway actually executed* (replayed from audit.jsonl) match expectations?

A decider is `async (client, settings, question, search_results) -> tool name`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic
from fastmcp import Client

from .config import ROOT, Settings

Decider = Callable[[Client, Settings, str, list[dict]], Awaitable[str]]

CASES = ROOT / "eval" / "routing_cases.jsonl"
SCENARIOS = ROOT / "eval" / "trajectories.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ------------------------------------------------------------------- deciders

async def top1_decider(client: Client, settings: Settings, question: str, results: list[dict]) -> str:
    """Baseline: trust the retrieval ranking."""
    return results[0]["name"] if results else "NONE"


async def llm_prompt_decider(client: Client, settings: Settings, question: str, results: list[dict]) -> str:
    """Render the server's `route_question` MCP Prompt, then let Claude answer it.

    The prompt (global routing policy + candidate cards) is authored by the server, so the
    eval exercises the exact artefact a real client would be handed.
    """
    prompt = await client.get_prompt("route_question", {"question": question, "k": str(len(results) or 4)})
    text = "\n".join(m.content.text for m in prompt.messages if getattr(m.content, "text", None))
    resp = await AsyncAnthropic(api_key=settings.anthropic_api_key).messages.create(
        model=settings.agent_model, max_tokens=40, messages=[{"role": "user", "content": text}]
    )
    out = "".join(b.text for b in resp.content if b.type == "text").strip()
    return out.split()[0] if out else "NONE"


# -------------------------------------------------------------------- routing

async def run_routing(client: Client, settings: Settings, cases: list[dict], decider: Decider, k: int = 4) -> dict:
    rows = []
    for case in cases:
        res = (await client.call_tool("search_tools", {"query": case["q"], "k": k})).data["results"]
        names = [r["name"] for r in res]
        chosen = await decider(client, settings, case["q"], res) if res else "NONE"
        ok_set = {case["expected"], *case.get("acceptable", [])}
        rows.append({**case, "chosen": chosen, "correct": chosen in ok_set, "candidates": names,
                     "expected_rank": names.index(case["expected"]) + 1 if case["expected"] in names else None})
    n = len(rows)
    return {
        "k": k, "rows": rows,
        "accuracy": sum(r["correct"] for r in rows) / n,
        "recall_at_k": sum(r["expected_rank"] is not None for r in rows) / n,
    }


# ----------------------------------------------------------------- trajectory

@dataclass
class TrajectoryResult:
    scenario: dict
    chosen: str
    sequence: list[str]
    passed: bool
    problems: list[str]


def _audit_since(path: Path, offset: int) -> list[dict]:
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read().decode("utf-8")
    return [json.loads(l) for l in data.splitlines() if l.strip()]


async def run_trajectory(client: Client, settings: Settings, scenario: dict, decider: Decider, k: int = 4) -> TrajectoryResult:
    """Run discover -> decide -> invoke, then check the executed tool sequence from the audit log."""
    audit = settings.log_dir / "audit.jsonl"
    offset = audit.stat().st_size if audit.exists() else 0

    res = (await client.call_tool("search_tools", {"query": scenario["q"], "k": k})).data["results"]
    chosen = await decider(client, settings, scenario["q"], res) if res else "NONE"
    if chosen != "NONE":
        args: dict[str, Any] = scenario.get("args", {"question": scenario["q"]})
        try:
            await client.call_tool("call_tool", {"name": chosen, "arguments": args})
        except Exception:  # failures are judged from the audit log below
            pass

    entries = _audit_since(audit, offset)
    # `call_tool` is the meta wrapper; the logical trajectory is the tools it dispatched.
    sequence = [e["tool"] for e in entries if e["tool"] != "call_tool"]
    problems: list[str] = []
    if sequence != scenario["expect_tools"]:
        problems.append(f"sequence {sequence} != expected {scenario['expect_tools']}")
    for bad in scenario.get("forbid", []):
        if any(_match(bad, t) for t in sequence):
            problems.append(f"forbidden tool used: {bad}")
    if len(sequence) > scenario.get("max_calls", 4):
        problems.append(f"too many calls: {len(sequence)}")
    failed = [e["tool"] for e in entries if not e["ok"]]
    if failed:
        problems.append(f"failed calls: {failed}")
    return TrajectoryResult(scenario, chosen, sequence, not problems, problems)


def _match(pattern: str, name: str) -> bool:
    from fnmatch import fnmatchcase
    return fnmatchcase(name, pattern)
