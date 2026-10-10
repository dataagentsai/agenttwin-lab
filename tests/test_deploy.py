"""A person deploys (3.6): the refusals, the approval port's adapters, the
release pointer and its rollback, the day-to-day comparison and the canary's
reading. Table-driven and fast: git repos made here, canned records and canned
API answers. The whole loop is `test_loop.py`.
"""

from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from agenttwin_lab.__main__ import main
from agenttwin_lab.approval import AutoForTests, GitHubEnvironment, LocalApproval, NotATest
from agenttwin_lab.deploy import case, deploy, why_not
from agenttwin_lab.loop import arm, compare, split
from agenttwin_lab.ports import Decision, DeployCase
from agenttwin_lab.releases import NoRelease, Releases, checkout

ROOT = Path(__file__).resolve().parents[1]
ROOT_CAUSE = "calendar:carrier-marks-undelivered-parcels-delivered"


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Any]:
    """A repository with a base commit and a fix commit on a branch."""
    path = tmp_path / "agent"
    path.mkdir()
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.email", "t@t.invalid")
    sh(path, "git", "config", "user.name", "t")
    (path / "reply.txt").write_text("Order AB-1 is currently delivered.\n")
    sh(path, "git", "add", "-A")
    sh(path, "git", "commit", "-q", "-m", "base")
    base = sh(path, "git", "rev-parse", "HEAD").strip()
    sh(path, "git", "checkout", "-q", "-b", "fix/INC-0001")
    (path / "reply.txt").write_text("The carrier scanned AB-1 as delivered.\n")
    sh(path, "git", "commit", "-q", "-am", "fix")
    head = sh(path, "git", "rev-parse", "HEAD").strip()
    sh(path, "git", "checkout", "-q", "main")
    return {"path": path, "base": base, "head": head, "repos": {"agent": path}}


def fix_record(base: str, head: str) -> dict[str, Any]:
    """A fix record the lab would deploy: fixed, green, nothing weakened."""
    return {
        "format": "agenttwin-fix/v0",
        "incident": {"id": "INC-0001", "rule": "reply-true", "root": ROOT_CAUSE,
                     "day": "clothing-day-7", "seed": 7},
        "run": "t",
        "branch": "fix/INC-0001",
        "versions": {"agenttwin": "0.9.0", "agent": {"base": base, "head": head}},
        "outcome": "fixed",
        "why_a_person": [],
        "judged": {
            "fixed": True,
            "reasons_not_fixed": [],
            "scenario_first": {"fails_before": True, "passes_after": True},
            "gates": {"kind": "incident-scenarios", "passed": True,
                      "summary": "1 of 1 incident scenario(s) pass"},
            "rerun": {"clean": True, "same_root_raised": False},
        },
        "protected_paths_changed": [],
        "approval_required": [{"repo": "agent", "path": "spec.yaml"}],
        "refused_commits": [],
        "refusals": {"by_layer": {"permissions": {"stopped": 0, "passed": 0},
                                  "hook": {"stopped": 2, "passed": 0}}},
        "weakening": {"final": [], "summary": {"attempted": {"by_label": {"weakening": 3}}}},
        "honesty": {"flags": 1, "contradictions": [{"claim": "no-tests-changed"}],
                    "undisclosed_weakenings": []},
        "finished_at": "2026-10-11T10:00:00+00:00",
        "time_to_fix_s": 20.0,
    }  # fmt: skip


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def change(r: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        for p in parents:
            r = r[p]
        r[last] = value

    return change


# (row, change to a deployable record, a fragment of the refusal; None: deployable)
REFUSALS: list[tuple[str, Callable[[dict[str, Any]], None], str | None]] = [
    ("fixed, green, nothing weakened", lambda r: None, None),
    ("needs a person", _set("outcome", "needs-person"), "the outcome is 'needs-person'"),
    ("handed to a person", _set("outcome", "handed-to-person"), "not 'fixed'"),
    ("never started", _set("outcome", "not-started"), "the outcome is 'not-started'"),
    ("the judge did not find it fixed", _set("judged.fixed", False), "the judge did not find"),
    ("the gates failed", _set("judged.gates.passed", False), "the gates did not pass"),
    (
        "a protected path landed",
        _set("protected_paths_changed", [{"repo": "agent", "path": "scenarios/x.yaml"}]),
        "protected path(s) changed on the branch: agent/scenarios/x.yaml",
    ),
    (
        "a weakening landed",
        _set("weakening.final", [{"category": "spec-rule-narrowed", "label": "weakening"}]),
        "weakens a check or a statement: spec-rule-narrowed",
    ),
    (
        "an unclear edit is not a weakening",
        _set("weakening.final", [{"category": "expectation-changed", "label": "unclear"}]),
        None,
    ),
    ("a refused commit", _set("refused_commits", [{"repo": "agent"}]), "pre-commit hook refused"),
    ("not a fix record", _set("format", "agenttwin-lab-day/v0"), "not a fix record"),
]


@pytest.mark.parametrize(("row", "change", "refused"), REFUSALS, ids=[r[0] for r in REFUSALS])
def test_deploy_is_refused_for_each_bad_outcome(
    repo: dict[str, Any], row: str, change: Callable[[dict[str, Any]], None], refused: str | None
) -> None:
    record = fix_record(repo["base"], repo["head"])
    change(record)
    reasons = why_not(record, {"agent": repo["base"]}, repo["repos"])
    if refused is None:
        assert reasons == []
    else:
        assert any(refused in r for r in reasons), reasons


# (row, production, the fix's base and head, a fragment of the refusal)
POINTERS = [
    ("stale: production moved since the fix was made", "head", ("base", "head"), "stale"),
    ("nothing moved", "base", ("base", "base"), "nothing to deploy"),
    ("a head the repository does not hold", "base", ("base", "f" * 40), "does not hold"),
]


@pytest.mark.parametrize(("row", "production", "move", "refused"), POINTERS,
                         ids=[p[0] for p in POINTERS])  # fmt: skip
def test_deploy_is_refused_against_the_pointer(
    repo: dict[str, Any], row: str, production: str, move: tuple[str, str], refused: str
) -> None:
    sha = {"base": repo["base"], "head": repo["head"]}
    record = fix_record(sha.get(move[0], move[0]), sha.get(move[1], move[1]))
    assert any(refused in r for r in why_not(record, {"agent": sha[production]}, repo["repos"]))


class Never:
    name = "never"

    def ask(self, case: DeployCase) -> Decision:
        raise AssertionError("a refused fix must not reach a person")


def test_a_refused_deploy_asks_nobody_and_moves_nothing(
    repo: dict[str, Any], tmp_path: Path
) -> None:
    releases = Releases(tmp_path / "releases")
    releases.init({"agent": repo["base"]}, by="t", note="t")
    record = fix_record(repo["base"], repo["head"])
    record["outcome"] = "needs-person"
    path = tmp_path / "fix.json"
    path.write_text(json.dumps(record))
    result = deploy(path, releases, Never(), repo["repos"])
    assert (result["status"], result["deployed"]) == ("refused", False)
    assert releases.current() == {"agent": repo["base"]}
    assert releases.pointer()["records"] == ["R0"]


# --------------------------------------------------------------------------- the local adapter

# (what the person types, approved?)
TYPED: list[tuple[str, str | type[BaseException], bool]] = [
    ("the incident id", "INC-0001", True),
    ("the id with spaces", "  INC-0001 \n", True),
    ("yes", "yes", False),
    ("another incident", "INC-0002", False),
    ("nothing", "", False),
    ("end of input", EOFError, False),
]


@pytest.mark.parametrize(("row", "typed", "approved"), TYPED, ids=[t[0] for t in TYPED])
def test_the_local_adapter_approves_only_the_typed_incident_id(
    repo: dict[str, Any], row: str, typed: str | type[BaseException], approved: bool
) -> None:
    shown: list[str] = []
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        if isinstance(typed, type):
            raise typed()
        return typed

    adapter = LocalApproval(read=read, write=shown.append, who="owner")
    decision = adapter.ask(case(fix_record(repo["base"], repo["head"]), "fix.json", "R1"))
    assert decision.approved is approved
    assert decision.approver == ("owner" if approved else None)
    assert decision.adapter == "local" and not decision.test_only
    assert prompts == ["Type INC-0001 to deploy R1; anything else declines: "]


# (what the person must be shown before saying yes)
SHOWN = [
    ("the verdict", "scenario fails before True, passes after True"),
    ("the gates", "incident-scenarios: 1 of 1 incident scenario(s) pass"),
    ("the refusals per layer", "hook 2"),
    ("the weakening tried", "(tried 3)"),
    ("the honesty flags", "honesty flags   1"),
    ("what each flag is", "contradiction: no-tests-changed"),
    ("what needs a person's approval", "agent/spec.yaml"),
    ("the move", "agent"),
]


@pytest.mark.parametrize(("row", "fragment"), SHOWN, ids=[s[0] for s in SHOWN])
def test_the_local_adapter_shows_the_case(repo: dict[str, Any], row: str, fragment: str) -> None:
    shown: list[str] = []
    LocalApproval(read=lambda _: "", write=shown.append).ask(
        case(fix_record(repo["base"], repo["head"]), "fix.json", "R1")
    )
    assert fragment in shown[0]


# --------------------------------------------------------------------------- the GitHub adapter

ACTIONS = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "dataagentsai/agenttwin-lab",
    "GITHUB_RUN_ID": "42",
    "AGENTTWIN_DEPLOY_ENVIRONMENT": "production",
}


def approval(login: str, kind: str = "User", state: str = "approved",
             env: str = "production") -> dict[str, Any]:  # fmt: skip
    return {"state": state, "user": {"login": login, "type": kind},
            "environments": [{"name": env}], "comment": ""}  # fmt: skip


# (row, environment, the run's approvals, approved?, approver)
GITHUB = [
    ("a reviewer approved", ACTIONS, [approval("basantchoudhary")], True, "basantchoudhary"),
    ("a bot approved", ACTIONS, [approval("deployer[bot]", "Bot")], False, None),
    ("rejected", ACTIONS, [approval("basantchoudhary", state="rejected")], False, None),
    ("approved for another environment", ACTIONS, [approval("x", env="staging")], False, None),
    ("no reviewers: nobody approved", ACTIONS, [], False, None),
    ("not inside GitHub Actions", {**ACTIONS, "GITHUB_ACTIONS": ""}, None, False, None),
    ("no environment named", {**ACTIONS, "AGENTTWIN_DEPLOY_ENVIRONMENT": ""}, None, False, None),
]


@pytest.mark.parametrize(("row", "env", "answers", "approved", "approver"), GITHUB,
                         ids=[g[0] for g in GITHUB])  # fmt: skip
def test_the_github_adapter_reads_the_environment_s_reviewers(
    repo: dict[str, Any],
    row: str,
    env: dict[str, str],
    answers: list[dict[str, Any]] | None,
    approved: bool,
    approver: str | None,
) -> None:
    asked: list[str] = []

    def api(path: str) -> Any:
        asked.append(path)
        return answers

    decision = GitHubEnvironment(env=env, api=api).ask(
        case(fix_record(repo["base"], repo["head"]), "fix.json", "R1")
    )
    assert (decision.approved, decision.approver) == (approved, approver)
    if answers is not None:
        assert asked == ["repos/dataagentsai/agenttwin-lab/actions/runs/42/approvals"]
    else:
        assert asked == [], "outside the deploy job nothing is asked of GitHub"


def test_the_deploy_workflow_waits_for_the_environment() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    on = workflow.get("on", workflow.get(True))
    assert "workflow_dispatch" in on and "fix_record" in on["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["deploy"]
    assert job["environment"] == "production"
    assert job["env"]["AGENTTWIN_DEPLOY_ENVIRONMENT"] == job["environment"]
    runs = [s.get("run", "") for s in job["steps"]]
    assert any("agenttwin-lab deploy" in r and "--approve github" in r for r in runs)
    assert not any("${{" in r for r in runs), "inputs reach scripts through env, never inline"


# --------------------------------------------------------------------------- auto-for-tests

# (row, PYTEST_CURRENT_TEST set, --i-am-a-test, allowed?)
AUTO = [
    ("outside pytest, no flag", False, False, False),
    ("outside pytest, the flag", False, True, True),
    ("under pytest", True, False, True),
]


@pytest.mark.parametrize(("row", "pytest_env", "flag", "allowed"), AUTO, ids=[a[0] for a in AUTO])
def test_auto_for_tests_is_refused_outside_tests(
    repo: dict[str, Any], row: str, pytest_env: bool, flag: bool, allowed: bool
) -> None:
    env = {"PYTEST_CURRENT_TEST": "t.py::t (call)"} if pytest_env else {}
    if not allowed:
        with pytest.raises(NotATest, match="always needs a person"):
            AutoForTests.allowed(flag, env)
        return
    decision = AutoForTests.allowed(flag, env).ask(
        case(fix_record(repo["base"], repo["head"]), "fix.json", "R1")
    )
    assert decision.approved and decision.test_only and decision.approver is None
    assert ("--i-am-a-test" if flag else "pytest") in decision.how


# (the command)
COMMANDS = [
    ["loop", "--approve", "auto-for-tests"],
    ["deploy", "fix.json", "--approve", "auto-for-tests"],
]


@pytest.mark.parametrize("argv", COMMANDS, ids=[c[0] for c in COMMANDS])
def test_the_commands_refuse_auto_for_tests_outside_tests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    with pytest.raises(SystemExit) as exited:
        main(argv)
    assert exited.value.code == 2
    assert "always needs a person" in capsys.readouterr().err


# --------------------------------------------------------------------------- the pointer


def test_deploy_moves_the_pointer_and_rollback_restores_it(
    repo: dict[str, Any], tmp_path: Path
) -> None:
    releases = Releases(tmp_path / "releases")
    releases.init({"agent": repo["base"]}, by="t", note="t")
    path = tmp_path / "fix.json"
    path.write_text(json.dumps(fix_record(repo["base"], repo["head"])))
    adapter = LocalApproval(read=lambda _: "INC-0001", write=lambda _: None, who="owner")
    result = deploy(path, releases, adapter, repo["repos"])
    release = result["release"]
    assert result["status"] == "deployed" and releases.current() == {"agent": repo["head"]}
    assert release["format"] == "agenttwin-release/v0" and release["id"] == "R1"
    assert release["approved_by"] == "owner" and release["approval"]["adapter"] == "local"
    assert release["repos"] == {"agent": {"base": repo["base"], "head": repo["head"]}}
    assert release["fix"]["gates"] == {
        "from": "fix.json#judged.gates", "kind": "incident-scenarios", "passed": True,
        "summary": "1 of 1 incident scenario(s) pass",
    }  # fmt: skip
    assert release["fix"]["incident"] == "INC-0001" and release["previous"] == "R0"

    assert main(["rollback", "--releases", str(releases.root), "--by", "owner"]) == 0
    back = releases.record("R2")
    assert releases.current() == {"agent": repo["base"]}
    assert (back["action"], back["rolled_back"], back["restored"], back["by"]) == (
        "rollback", "R1", "R0", "owner",
    )  # fmt: skip
    assert back["repos"] == {"agent": {"base": repo["head"], "head": repo["base"]}}
    assert releases.pointer()["stack"] == ["R0"]
    with pytest.raises(NoRelease, match="nothing to roll back"):
        releases.rollback(by="owner")


def test_a_declined_deploy_moves_nothing(repo: dict[str, Any], tmp_path: Path) -> None:
    releases = Releases(tmp_path / "releases")
    releases.init({"agent": repo["base"]}, by="t", note="t")
    path = tmp_path / "fix.json"
    path.write_text(json.dumps(fix_record(repo["base"], repo["head"])))
    adapter = LocalApproval(read=lambda _: "no", write=lambda _: None)
    result = deploy(path, releases, adapter, repo["repos"])
    assert result["status"] == "declined" and releases.current() == {"agent": repo["base"]}


@pytest.mark.parametrize("which", ["base", "head"])
def test_a_day_s_worktree_is_the_pointer_s_commit(
    repo: dict[str, Any], tmp_path: Path, which: str
) -> None:
    pointer = {"agent": repo[which]}
    with checkout(pointer, repo["repos"], {}, tmp_path / "work", "t") as trees:
        assert sh(trees["agent"], "git", "rev-parse", "HEAD").strip() == repo[which]
        said = (trees["agent"] / "reply.txt").read_text()
        assert ("carrier" in said) is (which == "head")
        where = trees["agent"]
    assert not where.exists(), "the release worktree is removed after the day"
    assert sh(repo["path"], "git", "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert str(where) not in sh(repo["path"], "git", "worktree", "list")


# --------------------------------------------------------------------------- day to day


def inc(root: str) -> dict[str, str]:
    return {"root": root}


# (row, day N roots, day N+1 roots, deployed roots, recurred, new, held)
COMPARE = [
    ("fixed and clean", ["a"], [], ["a"], [], [], True),
    ("not deployed, recurs", ["a"], ["a"], [], ["a"], [], False),
    ("deployed, recurs", ["a"], ["a"], ["a"], ["a"], [], False),
    ("deployed, a new one appears", ["a"], ["b"], ["a"], [], ["b"], False),
    ("one fixed, the other recurs", ["a", "b"], ["b"], ["a"], ["b"], [], True),
    ("clean both days", [], [], [], [], [], False),
]


@pytest.mark.parametrize(("row", "before", "after", "deployed", "recurred", "new", "held"),
                         COMPARE, ids=[c[0] for c in COMPARE])  # fmt: skip
def test_the_day_to_day_comparison(
    row: str,
    before: list[str],
    after: list[str],
    deployed: list[str],
    recurred: list[str],
    new: list[str],
    held: bool,
) -> None:
    got = compare([inc(r) for r in before], [inc(r) for r in after], deployed)
    assert (got["recurred"], got["new"], got["held"]) == (recurred, new, held)
    assert got["clean"] is (not after)


def ledger(visits: int, hit: dict[str, str]) -> dict[str, Any]:
    """A day with `visits` visits of one turn each; `hit` maps visit -> root met."""
    turns = [{"id": f"T{v}", "visit": f"V{v}"} for v in range(visits)]
    incidents: dict[str, dict[str, Any]] = {}
    for visit, root in hit.items():
        incidents.setdefault(root, {"root": root, "occurrences": []})["occurrences"].append(
            {"turn_id": "T" + visit.removeprefix("V")}
        )
    return {"turns": turns, "incidents": list(incidents.values())}


def _visits_in(arm_name: str, n: int = 40, seed: int = 7, fraction: float = 0.5) -> list[str]:
    return [f"V{v}" for v in range(n) if arm(f"V{v}", seed, fraction) == arm_name]


# (row, new release's day, old release's day, decision)
CANARY: list[tuple[str, Callable[[], dict[str, Any]], Callable[[], dict[str, Any]], str]] = [
    ("the new arm is clean", lambda: ledger(40, {}),
     lambda: ledger(40, dict.fromkeys(_visits_in("old")[:3], "a")), "promote"),
    ("the new arm meets the old root more often", lambda: ledger(40, dict.fromkeys(
        _visits_in("new")[:5], "a")), lambda: ledger(40, dict.fromkeys(_visits_in("old")[:1], "a")),
     "rollback"),
    ("the new arm meets a root the old day never did",
     lambda: ledger(40, {_visits_in("new")[0]: "b"}),
     lambda: ledger(40, dict.fromkeys(_visits_in("old")[:3], "a")), "rollback"),
    ("both clean", lambda: ledger(40, {}), lambda: ledger(40, {}), "promote"),
]  # fmt: skip


@pytest.mark.parametrize(("row", "new", "old", "decision"), CANARY, ids=[c[0] for c in CANARY])
def test_the_canary_reads_each_arm_from_its_own_day(
    row: str,
    new: Callable[[], dict[str, Any]],
    old: Callable[[], dict[str, Any]],
    decision: str,
) -> None:
    reading = split(new(), old(), 0.5, 7)
    assert reading["decision"] == decision
    arms = reading["arms"]
    assert arms["new"]["visits"] + arms["old"]["visits"] == 40
    assert arms["new"]["visits"] == len(_visits_in("new"))


def test_a_visit_always_goes_to_the_same_arm() -> None:
    assert [arm(f"V{v}", 7, 0.3) for v in range(50)] == [arm(f"V{v}", 7, 0.3) for v in range(50)]
    assert {arm(f"V{v}", 7, 0.0) for v in range(50)} == {"old"}
    assert {arm(f"V{v}", 7, 1.0) for v in range(50)} == {"new"}


def test_the_record_is_not_changed_by_the_case(repo: dict[str, Any]) -> None:
    record = fix_record(repo["base"], repo["head"])
    before = copy.deepcopy(record)
    case(record, "fix.json", "R1")
    why_not(record, {"agent": repo["base"]}, repo["repos"])
    assert record == before
