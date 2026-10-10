"""The lab's commands.

    uv run agenttwin-lab day --world worlds/clothing-day.yaml --repeat 3
    uv run agenttwin-lab brief INC-0001 --run runs/clothing-day-seed7-k2
    uv run agenttwin-lab fix INC-0001 --run runs/clothing-day-seed7-k2   # the stub, offline
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from agenttwin import load

from agenttwin_lab.brief import find, load_context, render
from agenttwin_lab.compose import GATE_BINDING, REFERENCE, default_lab
from agenttwin_lab.day import run_day
from agenttwin_lab.fixconfig import CONFIG, load_config
from agenttwin_lab.plan import load_plan
from agenttwin_lab.report import markdown, report

ROOT = Path(__file__).resolve().parents[2]


async def days(
    world_path: Path, plan_path: Path, seeds: list[int], binding: str, agent_root: Path
) -> list[dict[str, Any]]:
    plan = load_plan(plan_path)
    lab = default_lab(plan, binding=binding, agent_root=agent_root)
    world = load(world_path)
    return [await run_day(lab, world, plan, seed=seed) for seed in seeds]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenttwin-lab")
    sub = parser.add_subparsers(dest="command", required=True)
    day = sub.add_parser("day", help="run k seeded days and write the ledgers and the report")
    day.add_argument("--world", type=Path, default=ROOT / "worlds" / "clothing-day.yaml")
    day.add_argument("--plan", type=Path, default=ROOT / "days" / "one-day.yaml")
    day.add_argument("--seed", type=int, default=7)
    day.add_argument("--repeat", type=int, default=1, metavar="K", help="k seeded days: pass^k")
    day.add_argument("--out", type=Path, default=None)
    day.add_argument("--binding", default=GATE_BINDING)
    day.add_argument("--agent-root", type=Path, default=REFERENCE)

    brief = sub.add_parser("brief", help="turn an incident record into the fixer's brief")
    brief.add_argument("incident", help="the incident id, e.g. INC-0001")
    brief.add_argument("--run", type=Path, required=True, help="the day's run directory")
    brief.add_argument("--seed", type=int, default=None, help="which day, if several raised it")
    brief.add_argument("--world", type=Path, default=None, help="default: the fixer config's")
    brief.add_argument("--config", type=Path, default=CONFIG)
    brief.add_argument("--out", type=Path, default=None, help="write here instead of stdout")

    fix = sub.add_parser("fix", help="run the fixer on one incident, in git worktrees")
    fix.add_argument("incident", help="the incident id, e.g. INC-0001")
    fix.add_argument("--run", type=Path, required=True, help="the day's run directory")
    fix.add_argument("--seed", type=int, default=None)
    fix.add_argument("--config", type=Path, default=CONFIG)
    fix.add_argument("--adapter", choices=["stub", "real-claude"], default=None)
    fix.add_argument("--fixture", default=None, help="stub only: honest | gaming")
    fix.add_argument("--gates", choices=["incident-scenarios", "four-gates"], default=None)
    fix.add_argument("--max-turns", type=int, default=None)
    fix.add_argument("--open-pr", action="store_true", help="push the branch and open PRs (gh)")
    fix.add_argument("--keep-worktrees", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "brief":
        return _brief(args)
    if args.command == "fix":
        from agenttwin_lab.fixer import main_fix

        return main_fix(args)

    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    seeds = [args.seed + i for i in range(args.repeat)]
    ledgers = asyncio.run(days(args.world, args.plan, seeds, args.binding, args.agent_root))
    r = report(ledgers)
    out = args.out or ROOT / "runs" / f"{ledgers[0]['world']}-seed{args.seed}-k{args.repeat}"
    out.mkdir(parents=True, exist_ok=True)
    for ledger in ledgers:
        (out / f"ledger-{ledger['seed']}.json").write_text(json.dumps(ledger, indent=1) + "\n")
    (out / "report.json").write_text(json.dumps(r, indent=1) + "\n")
    (out / "report.md").write_text(markdown(r))
    print(markdown(r))
    print(f"written to {out}")
    return 0


def _brief(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    found = find(args.run, args.incident, seed=args.seed)
    text = render(
        found,
        world_file=args.world or config.world,
        context=load_context(),
        rules=config.rules(),
    )
    if args.out:
        args.out.write_text(text)
        print(f"written to {args.out}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
