"""Run the routing and trajectory evals and save a JSON report.

    uv run python scripts/eval_routing.py                  # routing, baseline top-1 retrieval decider
    uv run python scripts/eval_routing.py --llm            # routing, Claude answers the route_question MCP prompt
    uv run python scripts/eval_routing.py --trajectory     # discover -> decide -> invoke, checked from audit.jsonl
    uv run python scripts/eval_routing.py --trajectory --llm   # includes the scenarios marked live (calls Genie agents)

Reports go to eval/results/. A case passes if the chosen tool equals `expected` or is in `acceptable`.
"""

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime

from fastmcp import Client

from mcp_gateway.config import ROOT, load_settings
from mcp_gateway.evals import (CASES, SCENARIOS, llm_prompt_decider, load_jsonl, run_routing, run_trajectory,
                               top1_decider)
from mcp_gateway.gateway import build_server


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=4, help="candidates returned by search_tools")
    ap.add_argument("--llm", action="store_true", help="decider = Claude answering the route_question prompt")
    ap.add_argument("--trajectory", action="store_true", help="run trajectory scenarios instead of routing cases")
    args = ap.parse_args()

    settings = load_settings("meta")
    if args.llm and not settings.anthropic_api_key:
        raise SystemExit("--llm needs ANTHROPIC_API_KEY")
    decider = llm_prompt_decider if args.llm else top1_decider
    label = "llm" if args.llm else "top1-bm25"
    kind = "trajectory" if args.trajectory else "routing"

    async with Client(build_server(settings)) as c:
        if args.trajectory:
            scenarios = [s for s in load_jsonl(SCENARIOS) if args.llm or not s.get("live")]
            results = [await run_trajectory(c, settings, s, decider, args.k) for s in scenarios]
            for r in results:
                print(f"{'OK  ' if r.passed else 'FAIL'} {r.scenario['id']:<18} {r.sequence}  {'; '.join(r.problems)}")
            passed = sum(r.passed for r in results)
            print(f"\ntrajectory decider={label}  passed={passed}/{len(results)}")
            report = {"passed": passed, "total": len(results),
                      "rows": [{"id": r.scenario["id"], "chosen": r.chosen, "sequence": r.sequence,
                                "passed": r.passed, "problems": r.problems} for r in results]}
        else:
            rep = await run_routing(c, settings, load_jsonl(CASES), decider, args.k)
            for r in rep["rows"]:
                print(f"{'OK  ' if r['correct'] else 'MISS'} {r['chosen']:<26} (want {r['expected']}, "
                      f"rank {r['expected_rank']})  {r['q']}")
            print(f"\nrouting decider={label} k={args.k}  accuracy={rep['accuracy']:.2f}  recall@k={rep['recall_at_k']:.2f}")
            for (want, got), n in Counter((r["expected"], r["chosen"]) for r in rep["rows"] if not r["correct"]).most_common():
                print(f"  confused: {want} -> {got} x{n}")
            report = rep

    out = ROOT / "eval" / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"saved {out.relative_to(ROOT)}")


asyncio.run(main())
