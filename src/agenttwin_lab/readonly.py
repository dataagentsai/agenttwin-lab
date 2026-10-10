"""The read-only layer (slice 3.5): what the fixer may read but no tool may write.

- **Siblings** (agenttwin, the two catalogs) are no longer symlinks to the main
  checkouts. Each is a detached `git worktree` at the main checkout's HEAD, made
  read-only with `chmod -R a-w`, so a write that slips past every other layer
  fails with `EACCES` instead of landing in main. A sibling that is not a git
  repository is copied instead. Untracked `node_modules` is linked from main,
  read-only only by the rules (a known gap, written down here).
- **Yardstick files** in the fixer's own worktrees (the protected globs) are
  `chmod a-w` for the length of each attempt. A directory is locked too when
  the rule covers everything that could be made in it (`dir/**`, or `dir/*.ext`
  once the incoming directory exists), so a rename-over (`sed -i`, `mv`,
  `git apply`) fails as well. A protected file whose directory stays writable
  (`gates.yaml` in the repo root) can still be replaced by rename: the runner's
  diff is what catches that.

It is not a guarantee against a process of the same user: `chmod u+w` undoes it.
That is a shell command the hook and the deny rules see, and the runner's diff
reads the result either way.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agenttwin_lab.fixconfig import PathRule

WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def chmod_tree(root: Path, *, writable: bool) -> None:
    """`chmod -R a-w` (or `u+w`), never following a symlink out of the tree."""
    paths = [root]
    if root.is_dir() and not root.is_symlink():
        for here, dirs, files in os.walk(root, followlinks=False):
            paths += [Path(here) / n for n in (*dirs, *files)]
    for p in paths:
        set_writable(p, writable)


def set_writable(path: Path, writable: bool) -> None:
    if path.is_symlink() or not path.exists():
        return
    mode = path.stat().st_mode
    path.chmod((mode | stat.S_IWUSR) if writable else (mode & ~WRITE_BITS))


@dataclass
class SiblingCopy:
    name: str
    main: Path
    path: Path
    how: str
    """`git worktree --detach` or `copy`"""
    commit: str | None = None
    read_only: bool = True

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "how": self.how,
            "commit": self.commit,
            "read_only": self.read_only,
        }

    def dirty(self) -> list[str]:
        """Paths changed in the copy since it was made (a git copy only)."""
        if not self.how.startswith("git"):
            return []
        out = _git(self.path, "status", "--porcelain=v1", "--untracked-files=all", "--no-renames")
        return [line[3:] for line in out.stdout.splitlines() if line.strip()]

    def restore(self) -> None:
        chmod_tree(self.path, writable=True)
        _git(self.path, "checkout", "--", ".")
        _git(self.path, "clean", "-fdq")
        if self.read_only:
            chmod_tree(self.path, writable=False)

    def remove(self) -> None:
        if self.path.exists():
            chmod_tree(self.path, writable=True)
        if self.how.startswith("git"):
            _git(self.main, "worktree", "remove", "--force", str(self.path))
            _git(self.main, "worktree", "prune")
        shutil.rmtree(self.path, ignore_errors=True)


def sibling_copy(name: str, main: Path, dest: Path, *, lock: bool = True) -> SiblingCopy:
    """A read-only copy of a sibling at `dest`: a detached worktree at its
    HEAD (cheap, and the commit is on record), or a plain copy."""
    head = _git(main, "rev-parse", "HEAD")
    if head.returncode == 0:
        commit = head.stdout.strip()
        done = _git(main, "worktree", "add", "-q", "--detach", str(dest), commit)
        if done.returncode != 0:
            raise RuntimeError(f"git worktree add for {name}: {done.stderr.strip()}")
        copy = SiblingCopy(name, main, dest, "git worktree --detach", commit)
    else:
        shutil.copytree(main, dest, symlinks=True, ignore=shutil.ignore_patterns(".venv"))
        copy = SiblingCopy(name, main, dest, "copy")
    modules = main / "node_modules"
    if modules.is_dir() and not (dest / "node_modules").exists():
        (dest / "node_modules").symlink_to(modules)
    if lock:
        chmod_tree(dest, writable=False)
    copy.read_only = lock
    return copy


@dataclass
class Lock:
    """The yardstick files of the fixer's worktrees, read-only for one attempt."""

    paths: list[Path] = field(default_factory=list)

    def engage(self) -> None:
        for p in self.paths:
            set_writable(p, False)

    def release(self) -> None:
        for p in reversed(self.paths):
            set_writable(p, True)


def yardstick_paths(
    workdir: Path, rules: Iterable[PathRule], skip: Iterable[str] = ()
) -> list[Path]:
    """Every existing path under `workdir` a protected rule covers, plus each
    directory whose every possible new entry the rule would also cover.

    `skip` names top-level entries that are locked another way (the siblings)."""
    skipped = set(skip)
    out: set[Path] = set()
    for rule in rules:
        top = rule.path.split("/", 1)[0]
        if top in skipped or not (workdir / top).exists():
            continue
        prefix = rule.path.split("*", 1)[0].rstrip("/")
        base = workdir / prefix if "*" in rule.path else workdir / rule.path
        if "*" not in rule.path:
            if base.exists():
                out.add(base)
            continue
        start = base if base.is_dir() else base.parent
        if not start.is_dir():
            continue
        for here, dirs, files in os.walk(start, followlinks=False):
            for n in (*dirs, *files):
                p = Path(here) / n
                if rule.matches(str(p.relative_to(workdir))):
                    out.add(p)
        rest = rule.path[len(prefix) :].lstrip("/")
        if rest == "**":
            out.add(start)
            out.update(p for p in start.rglob("*") if p.is_dir() and not p.is_symlink())
        elif "/" not in rest and rest.startswith("*."):
            out.add(start)  # `dir/*.ext`: a new entry of that kind is protected too
    return sorted(out)


__all__ = ["Lock", "SiblingCopy", "chmod_tree", "set_writable", "sibling_copy", "yardstick_paths"]
