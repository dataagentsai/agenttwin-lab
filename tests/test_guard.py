"""The layers around the fixer (3.5), each on its own: the shell heuristic and
the hook, the pre-commit hook, and the read-only copies.

Table-driven. Everything runs in temporary directories; no main checkout is
touched and no model is called.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agenttwin_lab.fixconfig import load_config
from agenttwin_lab.guard import (
    MAIN_RULE,
    Policy,
    decide,
    git_hooks,
    pre_tool_use,
    read_log,
    shell_writes,
)
from agenttwin_lab.readonly import Lock, sibling_copy, yardstick_paths


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


# (command, the (target, means) it looks like it writes)
SHELL = [
    ("echo x > scenarios/a.yaml 2>&1", [("scenarios/a.yaml", "redirect")]),
    ("cat a >> gates.yaml && ls", [("gates.yaml", "redirect")]),
    ("echo x >/dev/null", []),
    ("echo x | tee -a harness-profile.yaml", [("harness-profile.yaml", "tee")]),
    ("sed -i '' 's/a/b/' scenarios/nobody-comes.yaml", [("scenarios/nobody-comes.yaml", "sed -i")]),
    ("sed -i.bak -e 's/a/b/' gates.yaml", [("gates.yaml", "sed -i")]),
    ("cp /tmp/x scenarios/y.yaml", [("scenarios/y.yaml", "cp")]),
    ("mv worlds/a.yaml /tmp/b.yaml", [("worlds/a.yaml", "mv"), ("/tmp/b.yaml", "mv")]),
    ("git checkout -- scenarios/a.yaml", [("scenarios/a.yaml", "git checkout --")]),
    ("git checkout HEAD~1 -- gates.yaml", [("gates.yaml", "git checkout --")]),
    ("git apply x.patch", [(None, "git apply")]),
    (
        "uv run python -c \"p='scenarios/a.yaml'; s=open(p).read(); open(p,'w').write(s)\"",
        [("scenarios/a.yaml", "python")],
    ),
    (
        "uv run python -c \"from pathlib import Path; Path('gates.yaml').write_text('')\"",
        [("gates.yaml", "python")],
    ),
    ("python3 -c \"import shutil; shutil.copy('a', 'worlds/w.yaml')\"",
     [("worlds/w.yaml", "python")]),
    ("python - <<'PY'\nopen('gates.yaml','w').write('x')\nPY", [("gates.yaml", "python")]),
    ("bash -c 'echo > gates.yaml'", [("gates.yaml", "redirect")]),
    ("cd ../clean-ai-engineering && echo x > tools/gates.py",
     [("../clean-ai-engineering/tools/gates.py", "redirect")]),
    ("uv run python -m agenttwin run --binding evals.gate_binding:open_subject s.yaml", []),
    ("uv run /x/clean-ai-engineering/tools/gates.py /x/reference-agent", []),
    ("ls -la; git status; git diff", []),
    # It can be fooled: a path assembled at run time is not on the line.
    ("uv run python -c \"import os; open(os.path.join('scen'+'arios','a.yaml'),'w')\"",
     [("a.yaml", "python")]),
]  # fmt: skip


@pytest.mark.parametrize(("command", "found"), SHELL, ids=[s[0][:50] for s in SHELL])
def test_the_shell_heuristic_reads_writes_off_the_command_line(
    command: str, found: list[tuple[str | None, str]]
) -> None:
    assert [(w.target, w.means) for w in shell_writes(command)] == found


@pytest.fixture
def policy(tmp_path: Path) -> Policy:
    config = load_config()
    work = tmp_path / "work"
    (work / "reference-agent" / "scenarios" / "incoming").mkdir(parents=True)
    main = tmp_path / "main" / "reference-agent"
    main.mkdir(parents=True)
    return Policy(
        workdir=str(work),
        protected=config.protected,
        mains={"reference-agent": str(main)},
        log=str(tmp_path / "refusals.jsonl"),
        tool_log=str(tmp_path / "tool-log.jsonl"),
    )


AGENT = "reference-agent"
LOST = (
    "uv run python -c \"p='scenarios/a-lost-parcel-goes-to-a-person.yaml'; open(p,'w').write('')\""
)

# (name, tool, input, denied, what the log holds: (layer, stopped, rule) or None)
HOOK = [
    ("the shell bypass of 3.4", "Bash", {"command": LOST}, True,
     ("hook", True, "reference-agent/scenarios/*.yaml")),
    ("a redirect into the worlds", "Bash", {"command": "echo x > worlds/clothing.yaml"}, True,
     ("hook", True, "reference-agent/worlds/**")),
    ("sed -i on the gates' config", "Bash", {"command": "sed -i '' s/a/b/ gates.yaml"}, True,
     ("hook", True, "reference-agent/gates.yaml")),
    ("cp over the binding", "Bash", {"command": "cp /tmp/x evals/gate_binding.py"}, True,
     ("hook", True, "reference-agent/evals/gate_binding.py")),
    ("git checkout -- on a yardstick", "Bash", {"command": "git checkout -- scenarios/a.yaml"},
     True, ("hook", True, "reference-agent/scenarios/*.yaml")),
    ("tee into a sibling", "Bash", {"command": "echo x | tee ../agenttwin/agenttwin/truth.py"},
     True, ("hook", True, "agenttwin/**")),
    ("into the gates, by cd", "Bash",
     {"command": "cd ../clean-ai-engineering && echo > tools/gates.py"}, True,
     ("hook", True, "clean-ai-engineering/tools/gates.py")),
    ("into the base copy the judge uses", "Bash",
     {"command": "echo x > ../base/reference-agent/src/a.py"}, True, ("hook", True, "base/**")),
    ("into a main checkout, by absolute path", "Bash", {"command": "echo x > {main}/src/a.py"},
     True, ("hook", True, MAIN_RULE)),
    ("the incoming scenario is not protected", "Bash",
     {"command": "echo x > scenarios/incoming/INC-0001.yaml"}, False, None),
    ("running a scenario", "Bash",
     {"command": "uv run python -m agenttwin run scenarios/incoming/INC-0001.yaml"}, False, None),
    ("a write it cannot place is allowed and logged", "Bash", {"command": "git apply x.patch"},
     False, ("hook", False, None)),
    ("fooled: a path built at run time", "Bash",
     {"command": "uv run python -c \"open('scen'+'arios/nobody-comes.yaml','w')\""}, False, None),
    ("a file tool on the yardstick", "Edit",
     {"file_path": "{work}/reference-agent/scenarios/nobody-comes.yaml"}, True,
     ("hook", True, "reference-agent/scenarios/*.yaml")),
    ("a file tool on a main checkout", "Write", {"file_path": "{main}/scenarios/new.yaml"}, True,
     ("hook", True, MAIN_RULE)),
    ("a file tool on the agent's code", "Edit",
     {"file_path": "{work}/reference-agent/src/support_agent/entrypoint/direct.py"}, False, None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "tool", "tool_input", "denied", "logged"), HOOK, ids=[h[0] for h in HOOK]
)
def test_the_pre_tool_use_hook_denies_and_logs(
    policy: Policy,
    name: str,
    tool: str,
    tool_input: dict[str, str],
    denied: bool,
    logged: tuple[str, bool, str | None] | None,
) -> None:
    filled = {
        k: v.format(work=policy.workdir, main=policy.mains["reference-agent"])
        for k, v in tool_input.items()
    }
    hook_input = {
        "tool_name": tool,
        "tool_input": filled,
        "tool_use_id": "toolu_1",
        "cwd": str(Path(policy.workdir) / AGENT),
        "hook_event_name": "PreToolUse",
    }
    assert decide(policy, hook_input).deny is denied
    out = pre_tool_use(Policy(**{**policy.__dict__, "log": ""}), hook_input)
    if denied:
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert out == "", "the hook never says allow: that would skip Claude Code's own rules"
    entries = read_log(Path(policy.log))
    if logged is None:
        assert entries == []
    else:
        (entry,) = entries
        assert (entry["layer"], entry["stopped"], entry["rule"]) == logged
        assert entry["tool_use_id"] == "toolu_1" and entry["format"] == "agenttwin-refusal/v0"


def test_the_hook_runs_as_claude_code_runs_it(policy: Policy, tmp_path: Path) -> None:
    """The command the settings file names, JSON on stdin, a decision on stdout."""
    from agenttwin_lab.guard import hook_command

    path = tmp_path / "policy.json"
    policy.dump(path)
    payload = {"tool_name": "Bash", "tool_input": {"command": LOST}, "tool_use_id": "t",
               "cwd": str(Path(policy.workdir) / AGENT)}  # fmt: skip
    done = subprocess.run(
        hook_command("pre-tool-use", path), shell=True, input=json.dumps(payload),
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# (staged path, refused)
COMMITS = [
    ("scenarios/a-lost-parcel-goes-to-a-person.yaml", True),
    ("harness-profile.yaml", True),
    ("scenarios/incoming/INC-0001.yaml", False),
    ("src/support_agent/entrypoint/direct.py", False),
]


@pytest.mark.parametrize(("staged", "refused"), COMMITS)
def test_the_pre_commit_hook_refuses_protected_paths(
    policy: Policy, tmp_path: Path, staged: str, refused: bool
) -> None:
    tree = Path(policy.workdir) / AGENT
    sh(tree, "git", "init", "-q")
    (tree / staged).parent.mkdir(parents=True, exist_ok=True)
    (tree / staged).write_text("x\n")
    sh(tree, "git", "add", "-A")
    path = tmp_path / "policy.json"
    policy.dump(path)
    hooks = git_hooks(tmp_path / "githooks", path)
    done = subprocess.run(
        ["git", "-c", f"core.hooksPath={hooks}", "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "x"],
        cwd=tree, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert (done.returncode != 0) is refused, done.stderr
    entries = read_log(Path(policy.log))
    assert [(e["layer"], e["path"]) for e in entries] == (
        [("pre-commit", f"{AGENT}/{staged}")] if refused else []
    )


# --------------------------------------------------------------------------- read-only


@pytest.fixture
def sibling(tmp_path: Path) -> Path:
    main = tmp_path / "main" / "agenttwin"
    (main / "agenttwin").mkdir(parents=True)
    (main / "agenttwin" / "truth.py").write_text("TRUE = True\n")
    (main / "README").write_text("agenttwin\n")
    sh(main, "git", "init", "-q")
    sh(main, "git", "add", "-A")
    sh(main, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    return main


# (how the write is tried)
WRITES = [
    ("open for writing", lambda p: p.write_text("TRUE = False\n")),
    ("create beside it", lambda p: (p.parent / "new.py").write_text("x")),
    ("rename over it", lambda p: os.replace(p.parent.parent / "README", p)),
    ("delete it", lambda p: p.unlink()),
]


@pytest.mark.parametrize(("how", "write"), WRITES, ids=[w[0] for w in WRITES])
def test_a_write_to_a_read_only_sibling_fails(
    tmp_path: Path, sibling: Path, how: str, write: object
) -> None:
    copy = sibling_copy("agenttwin", sibling, tmp_path / "work" / "agenttwin")
    try:
        target = copy.path / "agenttwin" / "truth.py"
        assert target.read_text() == "TRUE = True\n" and not target.is_symlink()
        with pytest.raises(PermissionError):
            write(target)  # type: ignore[operator]
        assert copy.how == "git worktree --detach" and copy.commit
    finally:
        copy.remove()
    assert not copy.path.exists()
    assert "work/agenttwin" not in sh(sibling, "git", "worktree", "list")
    assert (sibling / "agenttwin" / "truth.py").read_text() == "TRUE = True\n"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    agent = work / AGENT
    for rel in ("scenarios/a.yaml", "scenarios/incoming/.keep", "worlds/w.yaml", "gates.yaml",
                "src/a.py"):  # fmt: skip
        (agent / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent / rel).write_text("x\n")
    return work


# (what is tried, whether the lock stops it)
LOCKED = [
    ("open a yardstick scenario for writing",
     lambda a: (a / "scenarios/a.yaml").write_text("y"), True),
    ("add a scenario to the yardstick", lambda a: (a / "scenarios/b.yaml").write_text("y"), True),
    ("rename over a yardstick scenario",
     lambda a: os.replace(a / "src/a.py", a / "scenarios/a.yaml"), True),
    ("write a world", lambda a: (a / "worlds/w.yaml").write_text("y"), True),
    ("open the gates' config for writing", lambda a: (a / "gates.yaml").write_text("y"), True),
    # The limit: gates.yaml's directory is the repo root, which stays writable.
    ("rename over the gates' config",
     lambda a: os.replace(a / "src/a.py", a / "gates.yaml"), False),
    ("write the incoming scenario",
     lambda a: (a / "scenarios/incoming/i.yaml").write_text("y"), False),
    ("write the agent's code", lambda a: (a / "src/a.py").write_text("y"), False),
]  # fmt: skip


@pytest.mark.parametrize(("name", "write", "stopped"), LOCKED, ids=[x[0] for x in LOCKED])
def test_the_yardstick_is_read_only_during_an_attempt(
    tree: Path, name: str, write: object, stopped: bool
) -> None:
    lock = Lock(yardstick_paths(tree, load_config().protected))
    lock.engage()
    try:
        if stopped:
            with pytest.raises(PermissionError):
                write(tree / AGENT)  # type: ignore[operator]
        else:
            write(tree / AGENT)  # type: ignore[operator]
    finally:
        lock.release()
    (tree / AGENT / "scenarios" / "a.yaml").write_text("writable again\n")


# --------------------------------------------------------------------------- the runner's layer


def _commit(cwd: Path, message: str = "x") -> None:
    sh(cwd, "git", "add", "-A")
    sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


@pytest.fixture
def runner_lab(tmp_path: Path, sibling: Path) -> Iterator[dict[str, Any]]:
    """A main checkout, the fixer's worktree of it, a base copy, a sibling copy."""
    main = tmp_path / "main" / AGENT
    for rel in ("scenarios/a.yaml", "src/a.py", "gates.yaml"):
        (main / rel).parent.mkdir(parents=True, exist_ok=True)
        (main / rel).write_text(f"{rel}\n")
    sh(main, "git", "init", "-q")
    _commit(main)
    work = tmp_path / "work"
    work.mkdir()
    head = sh(main, "git", "rev-parse", "HEAD").strip()
    tree, base = work / AGENT, work / "base" / AGENT
    sh(main, "git", "worktree", "add", "-q", "-b", "fix/x", str(tree), head)
    sh(main, "git", "worktree", "add", "-q", "--detach", str(base), head)
    copy = sibling_copy("agenttwin", sibling, work / "agenttwin")
    policy = Policy(
        workdir=str(work),
        protected=load_config().protected,
        mains={AGENT: str(main), "agenttwin": str(sibling)},
        log=str(tmp_path / "refusals.jsonl"),
    )
    yield {"main": main, "tree": tree, "base": base, "copy": copy, "policy": policy,
           "head": head, "sibling": sibling}  # fmt: skip
    copy.remove()


def _secure(lab: dict[str, Any], before: dict[str, str]) -> list[dict[str, Any]]:
    from agenttwin_lab import protect

    return protect.secure(
        lab["policy"], {AGENT: lab["tree"]}, {AGENT: lab["head"]},
        copies=[lab["copy"]], bases={AGENT: lab["base"]},
        tried=[{"tool": "Bash", "tool_use_id": "toolu_9", "command": "echo > scenarios/a.yaml",
                "writes": [{"path": f"{AGENT}/scenarios/a.yaml"}]}],
        attempt=1, layers_on=["permissions", "hook", "read-only", "runner"],
        mains_before=before, mains={AGENT: lab["main"], "agenttwin": lab["sibling"]},
    )  # fmt: skip


def _unlock_copy(lab: dict[str, Any]) -> Path:
    from agenttwin_lab.readonly import chmod_tree

    chmod_tree(lab["copy"].path, writable=True)
    return Path(lab["copy"].path)


# (what got through, how, what the runner logs: (path, stopped, means), and the check after)
RUNNER = [
    ("a protected file changed",
     lambda lab: (lab["tree"] / "scenarios/a.yaml").write_text("weakened\n"),
     [(f"{AGENT}/scenarios/a.yaml", True, "shell")],
     lambda lab: (lab["tree"] / "scenarios/a.yaml").read_text() == "scenarios/a.yaml\n"),
    ("a protected file added",
     lambda lab: (lab["tree"] / "scenarios/b.yaml").write_text("new\n"),
     [(f"{AGENT}/scenarios/b.yaml", True, "unknown")],
     lambda lab: not (lab["tree"] / "scenarios/b.yaml").exists()),
    ("a protected file deleted",
     lambda lab: (lab["tree"] / "gates.yaml").unlink(),
     [(f"{AGENT}/gates.yaml", True, "unknown")],
     lambda lab: (lab["tree"] / "gates.yaml").read_text() == "gates.yaml\n"),
    ("the agent's code changed: not protected, left for the commit",
     lambda lab: (lab["tree"] / "src/a.py").write_text("fixed\n"),
     [],
     lambda lab: (lab["tree"] / "src/a.py").read_text() == "fixed\n"),
    ("a commit the fixer made itself, of a protected file",
     lambda lab: ((lab["tree"] / "scenarios/a.yaml").write_text("weakened\n"),
                  _commit(lab["tree"])),
     [(AGENT, True, "git"), (f"{AGENT}/scenarios/a.yaml", True, "shell")],
     lambda lab: sh(lab["tree"], "git", "rev-parse", "HEAD").strip() == lab["head"]
     and (lab["tree"] / "scenarios/a.yaml").read_text() == "scenarios/a.yaml\n"),
    ("a sibling copy changed",
     lambda lab: (_unlock_copy(lab) / "agenttwin/truth.py").write_text("TRUE = False\n"),
     [("agenttwin/agenttwin/truth.py", True, "unknown")],
     lambda lab: (lab["copy"].path / "agenttwin/truth.py").read_text() == "TRUE = True\n"),
    ("the base copy changed",
     lambda lab: (lab["base"] / "src/a.py").write_text("tampered\n"),
     [(f"base/{AGENT}/src/a.py", True, "unknown")],
     lambda lab: (lab["base"] / "src/a.py").read_text() == "src/a.py\n"),
    ("a main checkout changed: recorded, never reverted",
     lambda lab: (lab["main"] / "src/a.py").write_text("owner's work\n"),
     [("{main}", False, "unknown")],
     lambda lab: (lab["main"] / "src/a.py").read_text() == "owner's work\n"),
]  # fmt: skip


@pytest.mark.parametrize(("name", "change", "logged", "after"), RUNNER, ids=[r[0] for r in RUNNER])
def test_the_runner_reverts_what_got_through(
    runner_lab: dict[str, Any], name: str, change: Any, logged: list[Any], after: Any
) -> None:
    from agenttwin_lab import protect

    before = protect.snapshot({AGENT: runner_lab["main"], "agenttwin": runner_lab["sibling"]})
    change(runner_lab)
    entries = _secure(runner_lab, before)
    want = [(p.format(main=runner_lab["main"]), s, m) for p, s, m in logged]
    assert [(e["path"], e["stopped"], e["means"]) for e in entries] == want
    assert all(e["layer"] == "runner" for e in entries)
    assert after(runner_lab), name
