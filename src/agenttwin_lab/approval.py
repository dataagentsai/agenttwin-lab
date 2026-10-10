"""A person's approval of a deploy (grant slice 3.6), behind the `Approval` port.

- `local` (a terminal): the person reads the fix record's verdict, the
  refusals per layer, the weakening and the honesty flags, then types the
  incident id.
- `github` (a GitHub Actions job): the deploy job runs in a GitHub
  **environment** with required reviewers; GitHub holds the job until a
  reviewer approves it, and the adapter reads who did from the run's approvals.
- `auto-for-tests` (pytest, or `--i-am-a-test`): nobody. Refused anywhere
  else, and every record it writes says so.

None of them decides whether a fix may be deployed: `deploy.why_not` does, before
a person is asked. A person can only say no to a deploy the lab would allow.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agenttwin_lab.ports import Decision, DeployCase

TEST_ENV = "PYTEST_CURRENT_TEST"


def now() -> str:
    return datetime.now(UTC).isoformat()


def render_case(case: DeployCase) -> str:
    """The case as a person reads it before saying yes."""
    refusals = (
        ", ".join(
            f"{layer} {c['stopped']}" + (f" ({c['passed']} let through)" if c["passed"] else "")
            for layer, c in case.refusals.items()
        )
        or "none"
    )
    weakening = ", ".join(f"{k} {v}" for k, v in case.weakening.items()) or "none on the branch"
    lines = [
        f"Deploy {case.incident} as release {case.release}?",
        f"  fix record      {case.fix_record}",
        f"  outcome         {case.outcome}",
        f"  verdict         {case.verdict}",
        f"  gates           {case.gates}",
        f"  refusals        {refusals}",
        f"  weakening       {weakening} (tried {case.weakening_tried})",
        f"  honesty flags   {case.honesty_flags}"
        + "".join(f"\n                  - {h}" for h in case.honesty),
        "  needs approval  " + (", ".join(case.approval_required) or "none"),
    ]
    for repo, move in case.repos.items():
        lines.append(f"  {repo:<15} {move['base'][:12]} -> {move['head'][:12]}")
    return "\n".join(lines)


@dataclass
class LocalApproval:
    """An interactive confirmation: the case is shown, and the person types the
    incident id to approve. Anything else, or end of input, declines."""

    read: Callable[[str], str] = input
    write: Callable[[str], None] = print
    who: str = field(default_factory=getpass.getuser)
    name: str = "local"

    def ask(self, case: DeployCase) -> Decision:
        self.write(render_case(case))
        try:
            typed = self.read(
                f"Type {case.incident} to deploy {case.release}; anything else declines: "
            )
        except EOFError:
            typed = ""
        approved = typed.strip() == case.incident
        return Decision(
            approved=approved,
            approver=self.who if approved else None,
            adapter=self.name,
            at=now(),
            how="typed the incident id" if approved else f"declined (typed {typed.strip()!r})",
        )


class NotATest(Exception):
    """`auto-for-tests` asked for outside pytest and without `--i-am-a-test`."""


def why_test_only(i_am_a_test: bool, env: Mapping[str, str] | None = None) -> str | None:
    """Why approving without a person is allowed here, or `None` if it is not."""
    env = os.environ if env is None else env
    if env.get(TEST_ENV):
        return f"pytest ({TEST_ENV} is set)"
    if i_am_a_test:
        return "--i-am-a-test"
    return None


@dataclass
class AutoForTests:
    """Approves without a person. Only under pytest or with `--i-am-a-test`;
    every decision it makes is marked `test_only`."""

    why: str
    name: str = "auto-for-tests"

    @classmethod
    def allowed(
        cls, i_am_a_test: bool = False, env: Mapping[str, str] | None = None
    ) -> AutoForTests:
        why = why_test_only(i_am_a_test, env)
        if why is None:
            raise NotATest(
                "auto-for-tests approves without a person: only under pytest or with "
                "--i-am-a-test. The real loop always needs a person (--approve local or github)."
            )
        return cls(why=why)

    def ask(self, case: DeployCase) -> Decision:
        return Decision(
            approved=True,
            approver=None,
            adapter=self.name,
            at=now(),
            how=f"approved without a person, for a test: {self.why}",
            test_only=True,
        )


def gh_api(path: str) -> Any:
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"gh api {path}: {done.stderr.strip()}")
    return json.loads(done.stdout or "null")


@dataclass
class GitHubEnvironment:
    """The deploy job of `.github/workflows/deploy.yml` runs in a GitHub
    environment with required reviewers, so GitHub holds it until a reviewer
    approves. This adapter runs inside that job and reads the approval back
    from the run (`GET /repos/{repo}/actions/runs/{id}/approvals`). No approval
    by a person for this environment means no: an environment without
    reviewers deploys nothing."""

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    api: Callable[[str], Any] = gh_api
    name: str = "github"

    def ask(self, case: DeployCase) -> Decision:
        print(render_case(case))
        environment = self.env.get("AGENTTWIN_DEPLOY_ENVIRONMENT", "")
        repo, run_id = self.env.get("GITHUB_REPOSITORY", ""), self.env.get("GITHUB_RUN_ID", "")
        detail: dict[str, object] = {"environment": environment, "repository": repo, "run": run_id}
        if self.env.get("GITHUB_ACTIONS") != "true" or not (environment and repo and run_id):
            return self._no("not inside the deploy workflow's job in a GitHub environment", detail)
        approvals = self.api(f"repos/{repo}/actions/runs/{run_id}/approvals") or []
        people = [
            a["user"]["login"]
            for a in approvals
            if a.get("state") == "approved"
            and a.get("user", {}).get("type") != "Bot"
            and environment in [e.get("name") for e in a.get("environments") or []]
        ]
        detail["approvals"] = approvals
        if not people:
            return self._no(
                f"no person approved this run for the {environment!r} environment: the owner "
                "must add required reviewers to it (README, 'Deploying from GitHub')",
                detail,
            )
        return Decision(
            approved=True,
            approver=people[-1],
            adapter=self.name,
            at=now(),
            how=f"approved as a required reviewer of the {environment!r} environment",
            detail=detail,
        )

    def _no(self, why: str, detail: dict[str, object]) -> Decision:
        return Decision(
            approved=False, approver=None, adapter=self.name, at=now(), how=why, detail=detail
        )


__all__ = [
    "AutoForTests",
    "GitHubEnvironment",
    "LocalApproval",
    "NotATest",
    "render_case",
    "why_test_only",
]
