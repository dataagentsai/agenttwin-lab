"""The server-side layer (3.5): `.github/workflows/protected-paths.yml`, its
decision run locally on canned API answers, for the lab and the two proposals.

Table-driven. `gh` is a fake on PATH that records its arguments: nothing here
reaches GitHub.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from agenttwin_lab.fixconfig import load_config

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "protected-paths.yml"
LISTS = {
    "agenttwin-lab": ROOT / ".github" / "protected-paths.txt",
    "reference-agent": ROOT / "proposals" / "reference-agent" / ".github" / "protected-paths.txt",
    "clean-ai-engineering": ROOT
    / "proposals"
    / "clean-ai-engineering"
    / ".github"
    / "protected-paths.txt",
}
PUSHED = "2026-10-11T10:00:00Z"
LABEL = "protected-change-approved"


def script() -> str:
    run = yaml.safe_load(WORKFLOW.read_text())["jobs"]["protected-paths"]["steps"][1]["run"]
    return str(run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0])


def labelled(*events: tuple[str, str, str, str]) -> str:
    return "".join("\t".join(e) + "\n" for e in events)


PERSON_AFTER = ("labeled", "owner", "User", "2026-10-11T11:00:00Z")
PERSON_BEFORE = ("labeled", "owner", "User", "2026-10-11T09:00:00Z")
BOT_AFTER = ("labeled", "fixer[bot]", "Bot", "2026-10-11T11:00:00Z")
TAKEN_OFF = ("unlabeled", "owner", "User", "2026-10-11T12:00:00Z")

# (name, repo, changed files, label events, action, passes, label taken off)
CASES = [
    ("nothing protected", "reference-agent", ["src/support_agent/entrypoint/direct.py"], "",
     "opened", True, False),
    ("a yardstick scenario, no label", "reference-agent",
     ["scenarios/a-lost-parcel-goes-to-a-person.yaml"], "", "opened", False, False),
    ("a yardstick scenario, a person approved after the push", "reference-agent",
     ["scenarios/a-lost-parcel-goes-to-a-person.yaml"], labelled(PERSON_AFTER), "labeled", True,
     False),
    ("approved by a bot", "reference-agent", ["harness-profile.yaml"], labelled(BOT_AFTER),
     "labeled", False, False),
    ("approved, then a new push", "reference-agent", ["gates.yaml"], labelled(PERSON_BEFORE),
     "synchronize", False, True),
    ("approved, then the label taken off", "reference-agent", ["worlds/clothing.yaml"],
     labelled(PERSON_AFTER, TAKEN_OFF), "unlabeled", False, False),
    ("an incoming scenario needs approval too", "reference-agent",
     ["scenarios/incoming/INC-0001-x.yaml"], "", "opened", False, False),
    ("the AOAS: a spec change is the owner's", "clean-ai-engineering",
     ["drafts/examples/support-agent.aoas.yaml"], "", "opened", False, False),
    ("the gates themselves", "clean-ai-engineering", ["tools/gates.py"], "", "opened", False,
     False),
    ("the lab's own guard", "agenttwin-lab", ["src/agenttwin_lab/guard.py"], "", "opened", False,
     False),
    ("the lab's check itself", "agenttwin-lab", [".github/workflows/protected-paths.yml"], "",
     "opened", False, False),
    ("the lab's README", "agenttwin-lab", ["README.md"], "", "opened", True, False),
]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "repo", "changed", "labels", "action", "passes", "taken_off"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_the_check_fails_a_protected_change_without_a_person_s_approval(
    tmp_path: Path,
    name: str,
    repo: str,
    changed: list[str],
    labels: str,
    action: str,
    passes: bool,
    taken_off: bool,
) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "protected-paths.txt").write_text(LISTS[repo].read_text())
    (tmp_path / "changed.txt").write_text("\n".join(changed) + "\n")
    (tmp_path / "labels.tsv").write_text(labels)
    (tmp_path / "last-push.txt").write_text(PUSHED + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "gh-calls.txt"}\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "REPO": f"dataagentsai/{repo}",
        "PR": "7",
        "ACTION": action,
        "LABEL": LABEL,
    }
    done = subprocess.run(
        ["python3", "-c", script()], cwd=tmp_path, env=env, capture_output=True, text=True,
        check=False,
    )  # fmt: skip
    assert (done.returncode == 0) is passes, done.stdout + done.stderr
    calls = (tmp_path / "gh-calls.txt").read_text() if (tmp_path / "gh-calls.txt").exists() else ""
    assert (f"--remove-label {LABEL}" in calls) is taken_off


# (repo, the fixer.yaml rules each list must carry, by their prefix)
DRIFT = [
    ("reference-agent", "reference-agent/"),
    ("clean-ai-engineering", "clean-ai-engineering/"),
]


@pytest.mark.parametrize(("repo", "prefix"), DRIFT)
def test_each_proposal_protects_what_the_fixer_protects(repo: str, prefix: str) -> None:
    config = load_config()
    wanted = {
        rule.path.removeprefix(prefix)
        for rule in (*config.protected, *config.approval_required)
        if rule.path.startswith(prefix)
    }
    listed = {
        line.split("#", 1)[0].strip()
        for line in LISTS[repo].read_text().splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert wanted <= listed, f"missing from the proposal: {sorted(wanted - listed)}"


def test_the_proposals_carry_the_lab_s_workflow_unchanged() -> None:
    for repo in ("reference-agent", "clean-ai-engineering"):
        proposed = ROOT / "proposals" / repo / ".github" / "workflows" / "protected-paths.yml"
        assert proposed.read_text() == WORKFLOW.read_text()
