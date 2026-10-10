"""The commands of 3.6 and 3.7: `deploy`, `rollback` and `loop`."""

from __future__ import annotations

import argparse
import getpass
import json
from datetime import UTC, datetime
from pathlib import Path

from agenttwin_lab.approval import NotATest, why_test_only
from agenttwin_lab.compose import approval, default_fixer
from agenttwin_lab.deploy import deploy as deploy_fix
from agenttwin_lab.fixconfig import load_config
from agenttwin_lab.loop import LoopSettings, markdown, run_loops
from agenttwin_lab.ports import Approval
from agenttwin_lab.releases import NoRelease, Releases, has_commit, heads
from agenttwin_lab.sandbox import make_sandbox

ROOT = Path(__file__).resolve().parents[2]


def _approval(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Approval:
    try:
        return approval(args.approve, i_am_a_test=args.i_am_a_test)
    except NotATest as exc:
        parser.error(str(exc))


def deploy(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    chosen = _approval(args, parser)
    config = load_config(args.config)
    releases = Releases(args.releases)
    if not releases.exists():
        releases.init(
            heads(config.repos),
            by=getpass.getuser(),
            note="production bootstrapped from the main checkouts' HEAD",
        )
        print(f"production starts at R0: the main checkouts' HEAD ({releases.pointer_path})")
    result = deploy_fix(args.fix_record, releases, chosen, config.repos)
    if result["status"] == "refused":
        print(f"refused: {result['incident']} is not deployed")
        for reason in result["refused"]:
            print(f"  - {reason}")
        return 1
    if result["status"] == "declined":
        print(f"declined: {result['decision']['how']}")
        return 1
    release = result["release"]
    print(f"deployed {result['incident']} as {release['id']}, approved by "
          f"{release['approved_by'] or release['approval']['how']}")  # fmt: skip
    for repo, move in release["repos"].items():
        print(f"  {repo}: {str(move['base'])[:12]} -> {str(move['head'])[:12]}")
    return 0


def rollback(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        record = Releases(args.releases).rollback(by=args.by or getpass.getuser(), why=args.why)
    except NoRelease as exc:
        print(f"refused: {exc}")
        return 1
    print(
        f"rolled back {record['rolled_back']}: production is {record['restored']} "
        f"({record['id']})"
    )
    return 0


def loop(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    chosen = _approval(args, parser)
    if args.days < 2:
        parser.error("--days must be at least 2: a fix is only seen to hold on the next day")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.canary is not None and not 0 < args.canary < 1:
        parser.error("--canary is a fraction between 0 and 1")
    config_path = make_sandbox(args.sandbox, args.config) if args.sandbox else args.config
    config = load_config(config_path)
    adapter, _, fixture = args.fixer.partition(":")
    fixer = default_fixer(config, adapter=adapter, fixture=fixture or None)
    production = Releases(args.releases)
    initial = heads(config.repos)
    if production.exists():
        pointer = production.current()
        if set(pointer) == set(config.repos) and all(
            has_commit(config.repos[n], sha) for n, sha in pointer.items()
        ):
            initial = pointer
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or ROOT / "runs" / f"loop-{args.fixer.replace(':', '-')}-{stamp}"
    test_only = why_test_only(args.i_am_a_test) if chosen.name == "auto-for-tests" else None
    settings = LoopSettings(
        world=(args.world or config.world).resolve(),
        plan=(args.plan or config.plan).resolve(),
        days=args.days,
        seed=args.seed,
        repeat=args.repeat,
        fixer=args.fixer,
        approval_note="a person approves each deploy" if test_only is None
        else "auto-for-tests: no person approved; allowed only under pytest or --i-am-a-test",
        out=out,
        canary=args.canary,
    )
    record = run_loops(settings, config, fixer, chosen, initial, test_only=test_only)
    print(markdown(record), end="")
    print(f"written to {out} (run.json, report.md)")
    print(json.dumps({k: record["summary"][k] for k in ("outcomes", "deploys")}))
    return 0


__all__ = ["deploy", "loop", "rollback"]
