"""Whether a fix is green, behind the `FixGates` port. Run by the runner on the
committed worktree, never on the fixer's word.

- `incident-scenarios`: the incident's new scenario(s) against the fixed
  agent. Seconds.
- `four-gates`: the same, then clean-ai-engineering's `tools/gates.py` from
  the **main** checkout against the fixed agent. Minutes (about 4 alone on
  this laptop, 30 or more beside other work).

`four-gates` runs the main checkout's gates script, so the judge and the
yardstick it reads (`gates/support-agent-clothing.yaml` → the main
reference-agent's `scenarios/*.yaml`) are never the fixer's copies: a
yardstick scenario edited in the worktree does not change what is measured.
The AOAS is the worktree's, because the agent's `gates.yaml` names it by a
path relative to the agent (`../clean-ai-engineering/...`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenttwin_lab.ports import GateResult


def agent_env(worktree: Path, pythonpath: Sequence[str]) -> dict[str, str]:
    """The lab's interpreter, with the worktree's packages first on the path:
    the fixed agent, not the one the lab has installed."""
    env = dict(os.environ)
    paths = [str(worktree / p) for p in pythonpath]
    env["PYTHONPATH"] = os.pathsep.join([*paths, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["UV_FROZEN"] = "1"
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def run_scenarios(
    worktree: Path, scenarios: Sequence[Path], *, binding: str, pythonpath: Sequence[str], out: Path
) -> dict[str, Any]:
    """`agenttwin run` on these files, in the worktree, with its code. Returns
    per-file status and the failing checks."""
    if not scenarios:
        return {"ran": 0, "results": {}, "passed": False, "why": "no scenario to run"}
    command = [
        sys.executable, "-m", "agenttwin", "run", "-q", "--binding", binding,
        "--out", str(out), *map(str, scenarios),
    ]  # fmt: skip
    done = subprocess.run(
        command,
        cwd=worktree,
        env=agent_env(worktree, pythonpath),
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    results: dict[str, Any] = {}
    if out.is_file():
        report = json.loads(out.read_text())
        for r in report.get("scenarios", []):
            name = Path(str(r.get("file") or r.get("scenario"))).name
            results[name] = {
                "status": r.get("status"),
                "failed_checks": r.get("failed_checks") or [],
                "error": r.get("error"),
            }
    passed = bool(results) and all(r["status"] == "passed" for r in results.values())
    return {
        "ran": len(scenarios),
        "results": results,
        "passed": passed,
        "exit_code": done.returncode,
        "stderr_tail": "" if results else done.stderr[-1500:],
    }


@dataclass
class IncidentScenarios:
    binding: str
    pythonpath: tuple[str, ...]
    out: Path
    name: str = "incident-scenarios"

    def check(self, agent_worktree: Path, scenarios: Sequence[Path]) -> GateResult:
        started = time.monotonic()
        ran = run_scenarios(
            agent_worktree,
            scenarios,
            binding=self.binding,
            pythonpath=self.pythonpath,
            out=self.out / "incident-scenarios.json",
        )
        n = len(ran["results"])
        good = sum(r["status"] == "passed" for r in ran["results"].values())
        return GateResult(
            kind=self.name,
            passed=ran["passed"],
            summary=f"{good} of {n} incident scenario(s) pass" if n else ran.get("why", "none ran"),
            detail={"scenarios": ran},
            duration_s=round(time.monotonic() - started, 3),
        )


@dataclass
class FourGates:
    """The incident scenarios, then the four gates from the main checkout."""

    script: Path
    binding: str
    pythonpath: tuple[str, ...]
    out: Path
    name: str = "four-gates"

    def check(self, agent_worktree: Path, scenarios: Sequence[Path]) -> GateResult:
        started = time.monotonic()
        first = IncidentScenarios(self.binding, self.pythonpath, self.out).check(
            agent_worktree, scenarios
        )
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "VIRTUAL_ENV")}
        env["UV_FROZEN"] = "1"
        # The worktree's environment, with the extras its suite needs; frozen so
        # the lock the fixer committed is the lock that runs.
        subprocess.run(
            ["uv", "sync", "--extra", "dev"],
            cwd=agent_worktree, env=env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        done = subprocess.run(
            ["uv", "run", str(self.script), str(agent_worktree)],
            cwd=agent_worktree, env=env, capture_output=True, text=True, check=False,
            timeout=7200,
        )  # fmt: skip
        verdict_path = agent_worktree / "reports" / "gates" / "verdict.json"
        verdict: Mapping[str, Any] = (
            json.loads(verdict_path.read_text()) if verdict_path.is_file() else {}
        )
        lines = [
            ln.strip() for ln in done.stdout.splitlines() if ln.strip().startswith(("PASS", "FAIL"))
        ]
        gates_passed = bool(verdict.get("passed"))
        (self.out / "gates-verdict.json").write_text(json.dumps(verdict, indent=1) + "\n")
        return GateResult(
            kind=self.name,
            passed=first.passed and gates_passed,
            summary=f"{first.summary}; " + ("; ".join(lines) or "the gates wrote no verdict"),
            detail={
                "scenarios": first.detail["scenarios"],
                "gates": lines,
                "script": str(self.script),
                "exit_code": done.returncode,
            },
            duration_s=round(time.monotonic() - started, 3),
        )


__all__ = ["FourGates", "IncidentScenarios", "agent_env", "run_scenarios"]
