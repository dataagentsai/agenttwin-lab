"""The runner's layer (slice 3.5): after the fixer stops, read the result.

This is the one layer that is a guarantee, because it does not care how a
change was made. After every attempt, before anything is committed:

1. **A commit the fixer made itself** (HEAD moved): undone with
   `git reset --soft`, so its changes are staged and the runner owns the commit.
2. **Every protected path changed** in the fixer's worktrees, by any means,
   compared with the runner's last commit: put back as it was, and recorded
   with the means it most likely came by (the file tool or the shell command
   that named it, from the transcript).
3. **The read-only siblings and the base copies**: anything changed there is
   put back too.
4. **The main checkouts**: their `git status` and diff before and after the
   attempt. A change is recorded, never reverted (it may be the owner's own
   work in progress), and the fix goes to a person.

It also writes the log entries for the two layers that cannot write their
own: Claude Code's deny rules (`permissions`, from the transcript's
`permission_denials`) and the read-only copies (`read-only`, from a tool's
`EACCES` error).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from agenttwin_lab.fixconfig import PathRule
from agenttwin_lab.guard import LAYERS, Policy, log_refusal, read_log
from agenttwin_lab.readonly import SiblingCopy

EACCES = re.compile(
    r"Permission denied|PermissionError|EACCES|Read-only file system|Operation not permitted"
)
SHELL_DENY = PathRule("<shell deny rule>", "a shell command the settings deny")
BEFORE_RUNNER = ("permissions", "hook", "read-only")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def show(tree: Path, rev: str, path: str) -> str | None:
    """A file at a commit, or `None` if it is not there."""
    if _git(tree, "cat-file", "-e", f"{rev}:{path}").returncode != 0:
        return None
    return _git(tree, "show", f"{rev}:{path}").stdout


def changes(tree: Path) -> list[str]:
    """Paths changed against HEAD: staged, unstaged and untracked."""
    out = _git(tree, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    return sorted({entry[3:] for entry in out.stdout.split("\0") if len(entry) > 3})


def snapshot(mains: Mapping[str, Path]) -> dict[str, str]:
    """A fingerprint of each main checkout's uncommitted state."""
    out = {}
    for name, main in mains.items():
        status = _git(main, "status", "--porcelain=v1", "--untracked-files=all").stdout
        diff = _git(main, "diff", "HEAD").stdout
        out[name] = hashlib.sha256((status + "\0" + diff).encode()).hexdigest()
    return out


def means_for(relative: str, tried: Sequence[Mapping[str, Any]]) -> tuple[str, str | None]:
    """How a change most likely got in: the last file-tool write of that path
    that was let through, else the last shell command that named it."""
    for t in reversed(tried):
        if t.get("writes") is True and t.get("path") == relative and not t.get("stopped_by"):
            return "file tool", t.get("tool_use_id")
    name = PurePosixPath(relative).name
    for t in reversed(tried):
        if t.get("tool") != "Bash":
            continue
        named = any(w.get("path") == relative for w in t.get("writes") or [])
        if named or name in str(t.get("command", "")):
            return "shell", t.get("tool_use_id")
    return "unknown", None


def derive(
    policy: Policy, tried: Sequence[Mapping[str, Any]], attempt: int
) -> list[dict[str, Any]]:
    """Log entries for the deny rules and the read-only copies, read from the
    transcript (neither can write a log of its own)."""
    out = []
    for t in tried:
        stopped = t.get("stopped_by")
        if stopped not in ("permissions", "read-only"):
            continue
        rule: PathRule | None
        if t.get("tool") == "Bash":
            rules = [w for w in t.get("writes") or [] if w.get("rule")]
            rule = (
                PathRule(rules[0]["rule"], rules[0]["why"])
                if rules
                else (SHELL_DENY if stopped == "permissions" else None)
            )
            path = rules[0]["path"] if rules else None
        else:
            path = t.get("path")
            rule = policy.rule_for(Path(policy.workdir) / path) if path else None
        out.append(
            log_refusal(
                policy.log, layer=stopped, stopped=True, rule=rule, tool=t.get("tool"),
                tool_use_id=t.get("tool_use_id"), command=t.get("command"), path=path,
                means="shell" if t.get("tool") == "Bash" else "file tool",
                detail="refused by Claude Code's permission rules" if stopped == "permissions"
                else "the write failed on a read-only path",
                attempt=attempt,
            )  # fmt: skip
        )
    return out


def secure(
    policy: Policy,
    trees: Mapping[str, Path],
    expected: Mapping[str, str],
    *,
    copies: Sequence[SiblingCopy],
    bases: Mapping[str, Path],
    tried: Sequence[Mapping[str, Any]],
    attempt: int,
    layers_on: Sequence[str],
    mains_before: Mapping[str, str],
    mains: Mapping[str, Path],
) -> list[dict[str, Any]]:
    """The runner's layer: revert every protected change, by any means. Returns
    the log entries it wrote."""
    got_past = [layer for layer in BEFORE_RUNNER if layer in layers_on]
    out = []
    for repo, tree in trees.items():
        head = _git(tree, "rev-parse", "HEAD").stdout.strip()
        if head != expected[repo]:
            _git(tree, "reset", "-q", "--soft", expected[repo])
            out.append(
                log_refusal(
                    policy.log, layer="runner", stopped=True, rule=None, tool="git commit",
                    path=repo, means="git", attempt=attempt,
                    detail=f"the fixer committed on its own ({head[:12]}); undone with "
                    "reset --soft, so the runner owns the commit",
                )  # fmt: skip
            )
        for path in changes(tree):
            relative = f"{repo}/{path}"
            rule = policy.rule_for(tree / path)
            if rule is None:
                continue
            before = show(tree, expected[repo], path)
            if before is None:
                _git(tree, "rm", "-q", "--cached", "--ignore-unmatch", "--", path)
                (tree / path).unlink(missing_ok=True)
            else:
                _git(tree, "checkout", expected[repo], "--", path)
            means, use = means_for(relative, tried)
            out.append(
                log_refusal(
                    policy.log, layer="runner", stopped=True, rule=rule, tool=None,
                    tool_use_id=use, path=relative, means=means, attempt=attempt,
                    detail="changed in the worktree; reverted before the commit "
                    + ("(it was new: removed)" if before is None else "(restored from the base)"),
                    extra={"got_past": got_past},
                )  # fmt: skip
            )
    for copy in copies:
        dirty = copy.dirty()
        if dirty:
            copy.restore()
        for path in dirty:
            relative = f"{copy.name}/{path}"
            means, use = means_for(relative, tried)
            out.append(
                log_refusal(
                    policy.log, layer="runner", stopped=True,
                    rule=policy.rule_for(copy.path / path), tool_use_id=use, path=relative,
                    means=means, attempt=attempt, extra={"got_past": got_past},
                    detail="changed in a read-only sibling copy; restored",
                )  # fmt: skip
            )
    for repo, base in bases.items():
        dirty = changes(base)
        if dirty:
            _git(base, "checkout", "--", ".")
            _git(base, "clean", "-fdq")
        for path in dirty:
            relative = f"base/{repo}/{path}"
            means, use = means_for(relative, tried)
            out.append(
                log_refusal(
                    policy.log, layer="runner", stopped=True,
                    rule=policy.rule_for(base / path), tool_use_id=use, path=relative,
                    means=means, attempt=attempt, extra={"got_past": got_past},
                    detail="changed in the base copy the judge uses; restored",
                )  # fmt: skip
            )
    after = snapshot(mains)
    for name, fingerprint in after.items():
        if mains_before.get(name) != fingerprint:
            out.append(
                log_refusal(
                    policy.log, layer="runner", stopped=False,
                    rule=PathRule("<main checkout>/**", f"the main checkout of {name}"),
                    path=str(mains[name]), means="unknown", attempt=attempt,
                    detail="the main checkout's uncommitted state changed during the attempt; "
                    "not reverted (it may be the owner's own work): a person looks",
                )  # fmt: skip
            )
    return out


def summarise(log: Path, layers_on: Sequence[str]) -> dict[str, Any]:
    """The refusal log, numbered, each entry linked to the later layer that
    caught what got past it."""
    entries = read_log(log)
    for n, e in enumerate(entries, 1):
        e["n"] = n
        e["caught_later_by"] = None
    for e in entries:
        if e["stopped"] or not e.get("tool_use_id"):
            continue
        later = next(
            (
                x
                for x in entries
                if x["n"] > e["n"] and x["stopped"] and x.get("tool_use_id") == e["tool_use_id"]
            ),
            None,
        )
        if later is not None:
            e["caught_later_by"] = later["layer"]
    by_layer = {
        layer: {
            "on": layer in layers_on or layer == "permissions",
            "stopped": sum(1 for e in entries if e["layer"] == layer and e["stopped"]),
            "passed": sum(1 for e in entries if e["layer"] == layer and not e["stopped"]),
        }
        for layer in LAYERS
    }
    return {"log": log.name, "entries": entries, "by_layer": by_layer, "count": len(entries)}


__all__ = ["EACCES", "changes", "derive", "means_for", "secure", "show", "snapshot", "summarise"]
