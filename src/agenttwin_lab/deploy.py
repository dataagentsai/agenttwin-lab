"""A person deploys (grant slice 3.6): gates pass, then a person presses deploy.

    agenttwin-lab deploy runs/<day>/fixes/INC-0001-<run>/fix.json            # asks you
    agenttwin-lab rollback                                                    # one action

1. **Refuse** what the lab will not deploy, and say why (`why_not`): an outcome
   other than `fixed`, a judge that did not find it fixed, a protected path or
   a weakening on the branch, a refused commit, a fix made on a commit that is
   no longer production, a head the repository does not hold.
2. **Ask a person** (`Approval` port), showing the verdict, the refusals per
   layer, the weakening and the honesty flags.
3. **Record it**: the release pointer moves (`releases.py`) and a release
   record says who approved, when, which fix, the base and head per repo, and
   the gates result, referenced from the fix record.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agenttwin_lab.ports import Approval, DeployCase
from agenttwin_lab.releases import Releases, has_commit

FIX_FORMAT = "agenttwin-fix/v0"


def moves(record: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """repo -> {base, head} from the fix record's versions."""
    return {
        name: {"base": v["base"], "head": v["head"]}
        for name, v in record.get("versions", {}).items()
        if isinstance(v, dict) and "base" in v
    }


def why_not(
    record: Mapping[str, Any], production: Mapping[str, str], repos: Mapping[str, Path]
) -> list[str]:
    """Every reason the lab refuses to deploy this fix. Empty: a person may be asked."""
    if record.get("format") != FIX_FORMAT:
        return [f"not a fix record: format is {record.get('format')!r}, not {FIX_FORMAT}"]
    reasons = []
    outcome = record.get("outcome")
    if outcome != "fixed":
        why = "; ".join(record.get("why_a_person") or []) or "the fixer did not fix it"
        reasons.append(f"the outcome is {outcome!r}, not 'fixed': {why}")
    judged = record.get("judged") or {}
    if not judged.get("fixed"):
        found = "; ".join(judged.get("reasons_not_fixed") or []) or "no verdict"
        reasons.append(f"the judge did not find it fixed: {found}")
    gates = judged.get("gates") or {}
    if gates and not gates.get("passed"):
        reasons.append(f"the gates did not pass: {gates.get('summary')}")
    protected = record.get("protected_paths_changed") or []
    if protected:
        paths = ", ".join(f"{f['repo']}/{f['path']}" for f in protected)
        reasons.append(f"protected path(s) changed on the branch: {paths}")
    weak = sorted(
        {
            e["category"]
            for e in (record.get("weakening") or {}).get("final") or []
            if e.get("label") == "weakening"
        }
    )
    if weak:
        reasons.append(f"the branch weakens a check or a statement: {', '.join(weak)}")
    if record.get("refused_commits"):
        reasons.append("the pre-commit hook refused a commit in the fix run")
    move = moves(record)
    if not any(m["base"] != m["head"] for m in move.values()):
        reasons.append("nothing to deploy: no repository moved from its base")
    for name, m in move.items():
        if name not in production:
            reasons.append(f"{name} is not in production's pointer")
        elif production[name] != m["base"]:
            reasons.append(
                f"stale: the fix was made on {name} {m['base'][:12]}, "
                f"production runs {production[name][:12]}"
            )
        if name in repos and not has_commit(repos[name], m["head"]):
            reasons.append(f"{name} does not hold the fix's head {m['head'][:12]}")
    return reasons


def case(record: Mapping[str, Any], record_path: str, release: str) -> DeployCase:
    judged = record.get("judged") or {}
    gates = judged.get("gates") or {}
    weakening = record.get("weakening") or {}
    weak: dict[str, int] = {}
    for e in weakening.get("final") or []:
        if e.get("label") == "weakening":
            weak[e["category"]] = weak.get(e["category"], 0) + 1
    tried = (weakening.get("summary") or {}).get("attempted", {}).get("by_label", {})
    honesty = record.get("honesty") or {}
    told = [f"contradiction: {c['claim']}" for c in honesty.get("contradictions") or []]
    told += [
        f"undisclosed weakening: {u['subject']}"
        for u in honesty.get("undisclosed_weakenings") or []
    ]
    rerun = judged.get("rerun") or {}
    return DeployCase(
        incident=record["incident"]["id"],
        fix_record=record_path,
        release=release,
        outcome=str(record.get("outcome")),
        verdict=(
            f"scenario fails before {judged.get('scenario_first', {}).get('fails_before')}, "
            f"passes after {judged.get('scenario_first', {}).get('passes_after')}; "
            f"day re-run clean {rerun.get('clean')}, raised again {rerun.get('same_root_raised')}"
        ),
        gates=f"{gates.get('kind')}: {gates.get('summary')} (passed {gates.get('passed')})",
        refusals={
            layer: {"stopped": c["stopped"], "passed": c["passed"]}
            for layer, c in (record.get("refusals") or {}).get("by_layer", {}).items()
        },
        weakening=weak,
        weakening_tried=int(tried.get("weakening", 0)),
        honesty_flags=int(honesty.get("flags", 0)),
        honesty=told,
        repos=moves(record),
        approval_required=[
            f"{f['repo']}/{f['path']}" for f in record.get("approval_required") or []
        ],
    )


def deploy(
    record_path: Path,
    releases: Releases,
    approval: Approval,
    repos: Mapping[str, Path],
) -> dict[str, Any]:
    """Deploy one fix: refused, declined, or a release record.

    Returns `{"deployed": bool, "refused": [...], "decision": ..., "release": ...}`."""
    record = json.loads(record_path.read_text())
    production = releases.current()
    reasons = why_not(record, production, repos)
    incident = (record.get("incident") or {}).get("id")
    base = {"fix_record": str(record_path), "incident": incident, "outcome": record.get("outcome")}
    if reasons:
        return {**base, "deployed": False, "status": "refused", "refused": reasons,
                "decision": None, "release": None}  # fmt: skip
    asked = case(record, str(record_path), releases.next_id())
    decision = approval.ask(asked)
    if not decision.approved:
        return {**base, "deployed": False, "status": "declined", "refused": [],
                "decision": asdict(decision), "release": None}  # fmt: skip
    judged = record.get("judged") or {}
    gates = judged.get("gates") or {}
    release = releases.deploy(
        repos={name: m["head"] for name, m in moves(record).items()},
        approval=asdict(decision),
        fix={
            "record": str(record_path),
            "incident": incident,
            "root": record["incident"].get("root"),
            "day": record["incident"].get("day"),
            "run": record.get("run"),
            "branch": record.get("branch"),
            "outcome": record.get("outcome"),
            "finished_at": record.get("finished_at"),
            "time_to_fix_s": record.get("time_to_fix_s"),
            "gates": {
                "from": f"{record_path.name}#judged.gates",
                "kind": gates.get("kind"),
                "passed": gates.get("passed"),
                "summary": gates.get("summary"),
            },
            "rerun_clean": (judged.get("rerun") or {}).get("clean"),
            "honesty_flags": (record.get("honesty") or {}).get("flags"),
        },
    )
    return {**base, "deployed": True, "status": "deployed", "refused": [],
            "decision": asdict(decision), "release": release}  # fmt: skip


__all__ = ["case", "deploy", "moves", "why_not"]
