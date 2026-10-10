"""The protected paths, as every layer that defends them reads them (grant slice 3.5).

One policy file per fix run (`policy.json`, beside the fix record) holds the
protected globs, the main checkouts, and where the refusal log is. Each layer
reads it on its own, so each can be switched off without the others noticing:

- **permissions**: Claude Code's deny rules (`--settings`). Blocks file-tool
  edits of protected paths and of the main checkouts, and the denied shell
  prefixes. Logged as `permissions`, derived from the transcript by the runner.
- **hook**: a Claude Code `PreToolUse` hook (this module). Blocks shell
  commands that look like writes to protected paths, and file-tool edits of
  them. Logged as `hook`, by the hook itself.
- **read-only**: `git worktree add --detach` and `chmod a-w` (`readonly.py`).
  Blocks writes into the siblings and the yardstick files, by any tool. Logged
  as `read-only`, derived by the runner from the tool's `EACCES` error.
- **runner**: the runner reads the result, not the method (`fixer.py`). Every
  protected change that got through is reverted before the commit. Logged as
  `runner`.
- **pre-commit**: a git `pre-commit` hook through `core.hooksPath` (this
  module). Refuses a commit, by anyone in the run's environment, that stages a
  protected path. Logged as `pre-commit`, by the hook itself.

The hook's shell reading is **a heuristic, and it can be fooled** (a path built
at run time, base64, a script file written first and run second). Its value is
the log of what it saw, not a guarantee; the runner's diff is the guarantee.

    python -m agenttwin_lab.guard pre-tool-use  --policy policy.json   < hook input
    python -m agenttwin_lab.guard post-tool-use --policy policy.json   < hook input
    python -m agenttwin_lab.guard pre-commit    --policy policy.json   (git runs it)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agenttwin_lab.fixconfig import PathRule

REFUSAL_FORMAT = "agenttwin-refusal/v0"
LAYERS = ("permissions", "hook", "read-only", "runner", "pre-commit")
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
ATTEMPT_ENV = "AGENTTWIN_FIX_ATTEMPT"
MAIN_RULE = "<main checkout>/**"


def _real(path: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


@dataclass(frozen=True)
class Policy:
    """What is protected, for one fix run. Written by the runner, read by every layer."""

    workdir: str
    protected: tuple[PathRule, ...]
    mains: dict[str, str] = field(default_factory=dict)
    """name → main checkout. Nothing in a fix run may write there."""
    log: str = ""
    """`refusals.jsonl`"""
    tool_log: str = ""
    """`tool-log.jsonl`, every tool call the PostToolUse hook saw"""

    def dump(self, path: Path) -> None:
        raw = asdict(self)
        raw["protected"] = [{"path": p.path, "why": p.why} for p in self.protected]
        path.write_text(json.dumps(raw, indent=1) + "\n")

    @classmethod
    def load(cls, path: Path) -> Policy:
        raw = json.loads(path.read_text())
        raw["protected"] = tuple(PathRule(**p) for p in raw["protected"])
        return cls(**raw)

    def relative(self, path: str | Path) -> str | None:
        """`path` relative to the work directory, or `None` if outside it."""
        real, root = _real(path), _real(self.workdir)
        if real == root:
            return ""
        if real.startswith(root + os.sep):
            return real[len(root) + 1 :]
        return None

    def rule_for(self, path: str | Path) -> PathRule | None:
        """The rule a write to `path` breaks, if any: a protected glob under the
        work directory, or any path in a main checkout."""
        relative = self.relative(path)
        if relative is not None:
            return next((p for p in self.protected if p.matches(relative)), None)
        real = _real(path)
        for name, main in self.mains.items():
            root = _real(main)
            if real == root or real.startswith(root + os.sep):
                return PathRule(
                    MAIN_RULE, f"the main checkout of {name}: a fix run never writes there"
                )
        return None


# --------------------------------------------------------------------------- the refusal log


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def attempt_from_env() -> int | None:
    value = os.environ.get(ATTEMPT_ENV, "")
    return int(value) if value.isdigit() else None


def log_refusal(
    log: str | Path,
    *,
    layer: str,
    stopped: bool,
    rule: PathRule | None,
    tool: str | None = None,
    tool_use_id: str | None = None,
    command: str | None = None,
    path: str | None = None,
    means: str | None = None,
    detail: str = "",
    attempt: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one line to `refusals.jsonl`. `stopped` is whether this layer
    prevented it; `False` means it got past this layer (and a later layer may
    have caught it: the fix record links them)."""
    if layer not in LAYERS:
        raise ValueError(f"no layer {layer!r}")
    entry: dict[str, Any] = {
        "format": REFUSAL_FORMAT,
        "at": now(),
        "attempt": attempt if attempt is not None else attempt_from_env(),
        "layer": layer,
        "stopped": stopped,
        "tool": tool,
        "tool_use_id": tool_use_id,
        "command": command,
        "path": path,
        "rule": rule.path if rule else None,
        "why": rule.why if rule else None,
        "means": means,
        "detail": detail,
        **(extra or {}),
    }
    if log:
        with open(log, "a") as out:
            out.write(json.dumps(entry) + "\n")
    return entry


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ----------------------------------------------------------------------- reading a shell command


@dataclass(frozen=True)
class Write:
    """A write a shell command looks like it makes."""

    target: str | None
    """The path as written in the command, resolved against its directory;
    `None` when the command writes but the heuristic cannot say where."""
    means: str
    """How: `redirect`, `tee`, `sed -i`, `cp`, `mv`, `git checkout --`, `python`, ..."""


PY_WRITE = re.compile(
    r"\bopen\(.*?,\s*(?:mode\s*=\s*)?['\"][rbt]*[wax+]"
    r"|\.write_(?:text|bytes)\("
    r"|\bshutil\."
    r"|\bos\.(?:remove|unlink|rename|replace|truncate|rmdir|open)\b"
    r"|\.unlink\("
    r"|\.rename\("
    r"|\.touch\("
)
PY_LITERAL = re.compile(r"""(?<![A-Za-z0-9_])(['"])((?:(?!\1).)+?)\1""")
PATHLIKE = re.compile(r"^[^\s;|&<>]*(?:/|\.[A-Za-z0-9]{1,6}$)[^\s;|&<>]*$")
SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
DEST_LAST = {"cp", "install", "rsync", "ln", "scp"}
ALL_ARGS = {"mv", "rm", "unlink", "truncate", "touch", "rmdir", "shred"}
INPLACE = {"sed", "perl", "gsed"}


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _segments(tokens: Sequence[str]) -> Iterator[tuple[list[str], list[str]]]:
    """Simple commands, each as (words, redirect targets)."""
    words: list[str] = []
    redirected: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in SEPARATORS:
            yield words, redirected
            words, redirected = [], []
        elif tok in REDIRECTS or (tok.startswith(">") and set(tok) <= {">", "|"}):
            if i + 1 < len(tokens):
                target = tokens[i + 1]
                if not target.startswith("&") and target != "/dev/null":
                    redirected.append(target)
                i += 1
        elif tok in (">&", "<", "<<", "<<<", "<&"):
            i += 1  # `2>&1`, input redirections: not writes
        else:
            words.append(tok)
        i += 1
    yield words, redirected


def _program(words: list[str]) -> tuple[str, list[str]]:
    """The program and its arguments, past env assignments and runners."""
    rest = list(words)
    while rest:
        head = rest[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", head) or head in ("sudo", "command", "env"):
            rest = rest[1:]
        elif head == "uv" and len(rest) > 1 and rest[1] == "run":
            rest = rest[2:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
        elif head == "--":
            rest = rest[1:]
        else:
            break
    if not rest:
        return "", []
    return os.path.basename(rest[0]), rest[1:]


def _plain(args: Iterable[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def shell_writes(command: str, cwd: str = "") -> list[Write]:
    """The writes a shell command looks like it makes, by a conservative reading.

    Sees: redirections (`>`, `>>`, `&>`), `tee`, `sed -i`/`perl -i`, `cp`,
    `mv`, `install`, `rsync`, `ln`, `rm`, `touch`, `truncate`, `dd of=`,
    `git checkout -- p`, `git restore p`, `git rm`/`git mv`, `sh -c '...'`
    (read again), and Python run with `-c` or a heredoc whose code opens a file
    for writing, calls `write_text`/`write_bytes`, `shutil`, `os.remove` and the
    like (its targets are the path-like string literals in the code).

    Does not see: a path assembled at run time, an encoded script, a script
    file written by one command and run by the next, a program that writes
    without saying so on its command line. A write it can tell is a write but
    cannot place is returned with `target=None`.
    """
    found: list[Write] = []
    here = cwd
    for words, redirected in _segments(_tokens(command)):
        out: list[Write] = [Write(t, "redirect") for t in redirected]
        prog, args = _program(words)
        if prog == "cd":
            moved = _plain(args)
            here = os.path.join(here, os.path.expanduser(moved[0])) if moved else here
            continue
        for w in _write_args(prog, args, command, out):
            target = os.path.join(here, w.target) if w.target is not None else None
            found.append(Write(target, w.means))
    return found


def _write_args(prog: str, args: list[str], command: str, out: list[Write]) -> list[Write]:
    if prog:
        if prog in ("sh", "bash", "zsh") and "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args):
                out += shell_writes(args[i + 1])
        elif prog == "tee":
            out += [Write(a, "tee") for a in _plain(args)]
        elif prog in INPLACE and any(
            a.startswith("-i")
            or a == "--in-place"
            or (a.startswith("-") and "i" in a[1:] and prog != "sed")
            for a in args
        ):
            plain, skip = [], False
            for a in args:
                if skip:
                    skip = False
                elif a in ("-e", "-f", "--expression", "--file"):
                    skip = True
                elif not a.startswith("-") and a != "":
                    plain.append(a)
            scripted = any(
                a in ("-e", "-f") or a.startswith(("--expression", "--file")) for a in args
            )
            targets = plain if scripted else plain[1:]
            out += [Write(a, f"{prog} -i") for a in targets]
        elif prog in DEST_LAST:
            plain = _plain(args)
            if plain:
                out.append(Write(plain[-1], prog))
        elif prog in ALL_ARGS:
            out += [Write(a, prog) for a in _plain(args)]
        elif prog in ("chmod", "chown", "chflags"):
            out += [Write(a, prog) for a in _plain(args)[1:]]
        elif prog == "dd":
            out += [Write(a[3:], "dd") for a in args if a.startswith("of=")]
        elif prog == "git":
            out += _git_writes(args)
        elif re.fullmatch(r"python[0-9.]*|pypy[0-9.]*", prog):
            out += _python_writes(command, args)
        elif prog == "find" and any(a in ("-delete", "-exec", "-execdir") for a in args):
            out.append(Write(None, "find"))
    return out


def _git_writes(args: list[str]) -> list[Write]:
    sub = next((a for a in args if not a.startswith("-")), "")
    rest = args[args.index(sub) + 1 :] if sub else []
    if sub == "checkout" and "--" in rest:
        return [Write(a, "git checkout --") for a in rest[rest.index("--") + 1 :]]
    if sub in ("restore", "rm", "mv"):
        paths = rest[rest.index("--") + 1 :] if "--" in rest else _plain(rest)
        return [Write(a, f"git {sub}") for a in paths]
    if sub in ("apply", "am", "stash", "clean") or (sub == "reset" and "--hard" in rest):
        return [Write(None, f"git {sub}")]
    if sub == "checkout":
        return [Write(None, "git checkout")]
    return []


def _python_writes(command: str, args: list[str]) -> list[Write]:
    if "-c" in args:
        i = args.index("-c")
        code = args[i + 1] if i + 1 < len(args) else ""
    elif "-" in args or "<<" in command:
        code = command  # a heredoc: the code is the rest of the command
    else:
        return []  # a module or a script file: nothing to read on the line
    if not PY_WRITE.search(code):
        return []
    literals = [m.group(2) for m in PY_LITERAL.finditer(code)]
    targets = [s for s in literals if PATHLIKE.match(s) and not s.startswith(("http:", "https:"))]
    return [Write(t, "python") for t in targets] or [Write(None, "python")]


def resolve(target: str, cwd: str) -> str:
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))


# --------------------------------------------------------------------------- the hooks


@dataclass(frozen=True)
class Decision:
    deny: bool
    reason: str
    entries: list[dict[str, Any]]


def decide(policy: Policy, hook_input: dict[str, Any]) -> Decision:
    """The PreToolUse hook's decision for one tool call, logged."""
    tool = str(hook_input.get("tool_name", ""))
    tool_input = hook_input.get("tool_input") or {}
    use_id = hook_input.get("tool_use_id")
    cwd = str(hook_input.get("cwd") or os.getcwd())
    entries: list[dict[str, Any]] = []
    reasons: list[str] = []
    if tool in FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        rule = policy.rule_for(resolve(path, cwd)) if path else None
        if rule:
            relative = policy.relative(resolve(path, cwd))
            entries.append(
                log_refusal(
                    policy.log,
                    layer="hook",
                    stopped=True,
                    rule=rule,
                    tool=tool,
                    tool_use_id=use_id,
                    path=relative if relative is not None else path,
                    means="file tool",
                    detail=f"{tool} of a protected path",
                )  # fmt: skip
            )
            reasons.append(f"{path} is protected ({rule.path}: {rule.why})")
    elif tool == "Bash":
        command = str(tool_input.get("command", ""))
        unplaced: list[Write] = []
        for w in shell_writes(command):
            if w.target is None:
                unplaced.append(w)
                continue
            where = resolve(w.target, cwd)
            rule = policy.rule_for(where)
            if rule is None:
                continue
            relative = policy.relative(where)
            entries.append(
                log_refusal(
                    policy.log,
                    layer="hook",
                    stopped=True,
                    rule=rule,
                    tool=tool,
                    tool_use_id=use_id,
                    command=command,
                    path=relative if relative is not None else where,
                    means=f"shell ({w.means})",
                    detail=f"the command looks like a write to a protected path ({w.means})",
                )  # fmt: skip
            )
            reasons.append(f"{w.target} is protected ({rule.path}: {rule.why})")
        if not reasons and unplaced:
            # A write it cannot place: allowed, and logged so a later layer's
            # catch can be traced back to it.
            entries.append(
                log_refusal(
                    policy.log,
                    layer="hook",
                    stopped=False,
                    rule=None,
                    tool=tool,
                    tool_use_id=use_id,
                    command=command,
                    means="shell (" + ", ".join(sorted({w.means for w in unplaced})) + ")",
                    detail="the command writes somewhere the hook cannot tell; allowed",
                )  # fmt: skip
            )
    reason = "; ".join(reasons)
    if reason:
        reason = (
            f"Blocked by the lab's protected-path hook: {reason}. Protected paths are the "
            "yardstick; route the change to a person instead."
        )
    return Decision(deny=bool(reasons), reason=reason, entries=entries)


def pre_tool_use(policy: Policy, hook_input: dict[str, Any]) -> str:
    """The hook's stdout: a `deny` decision, or nothing (never `allow`, which
    would skip Claude Code's own permission rules)."""
    decision = decide(policy, hook_input)
    if not decision.deny:
        return ""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }
    )


def post_tool_use(policy: Policy, hook_input: dict[str, Any]) -> None:
    """Every tool call, as Claude Code reports it after the fact."""
    if not policy.tool_log:
        return
    response = hook_input.get("tool_response", hook_input.get("error"))
    entry = {
        "at": now(),
        "attempt": attempt_from_env(),
        "event": hook_input.get("hook_event_name"),
        "tool": hook_input.get("tool_name"),
        "tool_use_id": hook_input.get("tool_use_id"),
        "input": hook_input.get("tool_input"),
        "response": response if len(json.dumps(response)) < 4000 else str(response)[:4000],
    }
    with open(policy.tool_log, "a") as out:
        out.write(json.dumps(entry, default=str) + "\n")


def pre_commit(policy: Policy, cwd: Path) -> tuple[bool, list[dict[str, Any]]]:
    """The git hook: refuse a commit that stages a protected path."""
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--no-renames"],
        cwd=cwd, capture_output=True, text=True, check=False,
    ).stdout.split("\n")  # fmt: skip
    entries = []
    for name in filter(None, staged):
        where = os.path.join(top, name)
        rule = policy.rule_for(where)
        if rule:
            relative = policy.relative(where)
            entries.append(
                log_refusal(
                    policy.log,
                    layer="pre-commit",
                    stopped=True,
                    rule=rule,
                    tool="git commit",
                    path=relative if relative is not None else where,
                    means="git",
                    detail="a commit staged a protected path",
                )  # fmt: skip
            )
    return not entries, entries


def git_hooks(directory: Path, policy_path: Path, python: str = sys.executable) -> Path:
    """A hooks directory holding the pre-commit hook, for `core.hooksPath`."""
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "# agenttwin-lab: refuse a commit that stages a protected path (slice 3.5).\n"
        f"exec {shlex.quote(python)} -m agenttwin_lab.guard pre-commit "
        f"--policy {shlex.quote(str(policy_path))}\n"
    )
    hook.chmod(0o755)
    return directory


def hook_command(event: str, policy_path: Path, python: str = sys.executable) -> str:
    policy = shlex.quote(str(policy_path))
    return f"{shlex.quote(python)} -m agenttwin_lab.guard {event} --policy {policy}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agenttwin_lab.guard")
    p.add_argument("event", choices=["pre-tool-use", "post-tool-use", "pre-commit"])
    p.add_argument("--policy", type=Path, required=True)
    args = p.parse_args(argv)
    policy = Policy.load(args.policy)
    if args.event == "pre-commit":
        ok, entries = pre_commit(policy, Path.cwd())
        for e in entries:
            print(
                f"agenttwin-lab: refused, {e['path']} is protected ({e['rule']})", file=sys.stderr
            )
        return 0 if ok else 1
    hook_input = json.loads(sys.stdin.read() or "{}")
    if args.event == "pre-tool-use":
        out = pre_tool_use(policy, hook_input)
        if out:
            print(out)
        return 0
    post_tool_use(policy, hook_input)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ATTEMPT_ENV",
    "LAYERS",
    "MAIN_RULE",
    "REFUSAL_FORMAT",
    "Decision",
    "Policy",
    "Write",
    "decide",
    "git_hooks",
    "hook_command",
    "log_refusal",
    "pre_commit",
    "pre_tool_use",
    "read_log",
    "shell_writes",
]
