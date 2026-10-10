"""The lab's "production" (grant slice 3.6): a release pointer, owned by the lab.

Production is not a branch of anyone's repository. It is a file in the lab,
`releases/production.json`, naming for each repo the commit the next simulated
day runs against. Each change to it is a release record, `releases/R<n>.json`
(`agenttwin-release/v0`). **The real repositories' `main` is never moved**: a
deploy only points the lab at a commit the fixer already made on its branch.

- `init`, `R0`: the repos' HEADs (or a pointer handed in); `stack = [R0]`.
- `deploy`, `R<n>`: who approved, when, which fix, the base and head per repo,
  the fix's gates result; pushed onto the stack.
- `rollback`, `R<n>`: who, when, which release it undid; popped, so
  production is the previous release again.

When a day starts it resolves the pointer to detached git worktrees (`checkout`)
and runs the agent from them, never from the main checkouts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RELEASE_FORMAT = "agenttwin-release/v0"
POINTER_FORMAT = "agenttwin-production/v0"


class NoRelease(Exception):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {done.stderr.strip()}")
    return done.stdout


def heads(repos: Mapping[str, Path]) -> dict[str, str]:
    return {name: git(path, "rev-parse", "HEAD").strip() for name, path in repos.items()}


def has_commit(repo: Path, sha: str) -> bool:
    done = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True, check=False
    )
    return done.returncode == 0


@dataclass(frozen=True)
class Releases:
    """The release pointer and its records, in one directory."""

    root: Path

    @property
    def pointer_path(self) -> Path:
        return self.root / "production.json"

    def exists(self) -> bool:
        return self.pointer_path.is_file()

    def pointer(self) -> dict[str, Any]:
        if not self.exists():
            raise NoRelease(f"{self.pointer_path} does not exist: no release yet (init first)")
        loaded: dict[str, Any] = json.loads(self.pointer_path.read_text())
        return loaded

    def current(self) -> dict[str, str]:
        """repo -> the commit production runs."""
        return dict(self.pointer()["repos"])

    def record(self, release: str) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads((self.root / f"{release}.json").read_text())
        return loaded

    def next_id(self) -> str:
        return f"R{len(self.pointer()['records'])}" if self.exists() else "R0"

    def init(self, repos: Mapping[str, str], *, by: str, note: str) -> dict[str, Any]:
        if self.exists():
            raise FileExistsError(f"{self.pointer_path} exists: production already has a release")
        record = {
            "format": RELEASE_FORMAT,
            "id": "R0",
            "action": "init",
            "at": _now(),
            "by": by,
            "note": note,
            "approval": None,
            "fix": None,
            "repos": {name: {"base": None, "head": sha} for name, sha in repos.items()},
            "previous": None,
        }
        self._write(record, stack=["R0"], records=["R0"], repos=dict(repos))
        return record

    def deploy(
        self,
        *,
        repos: Mapping[str, str],
        approval: Mapping[str, Any],
        fix: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Point production at `repos` and write the release record."""
        p = self.pointer()
        release = self.next_id()
        old = p["repos"]
        record = {
            "format": RELEASE_FORMAT,
            "id": release,
            "action": "deploy",
            "at": _now(),
            "approved_by": approval.get("approver"),
            "approval": dict(approval),
            "test_only": bool(approval.get("test_only")),
            "fix": dict(fix),
            "repos": {
                name: {"base": old.get(name), "head": repos.get(name, old.get(name))}
                for name in sorted({*old, *repos})
            },
            "previous": p["current"],
        }
        new = {name: move["head"] for name, move in record["repos"].items()}
        self._write(record, stack=[*p["stack"], release], records=[*p["records"], release],
                    repos=new)  # fmt: skip
        return record

    def rollback(self, *, by: str, why: str = "") -> dict[str, Any]:
        """Production goes back to the release before the current one."""
        p = self.pointer()
        if len(p["stack"]) < 2:
            raise NoRelease(
                f"production is {p['current']}, the first release: nothing to roll back"
            )
        undone, restored = p["stack"][-1], p["stack"][-2]
        to = {name: move["head"] for name, move in self.record(restored)["repos"].items()}
        release = self.next_id()
        record = {
            "format": RELEASE_FORMAT,
            "id": release,
            "action": "rollback",
            "at": _now(),
            "by": by,
            "why": why,
            "rolled_back": undone,
            "restored": restored,
            "repos": {
                name: {"base": p["repos"].get(name), "head": to.get(name)}
                for name in sorted({*p["repos"], *to})
            },
            "previous": p["current"],
        }
        self._write(record, stack=p["stack"][:-1], records=[*p["records"], release], repos=to,
                    current=restored)  # fmt: skip
        return record

    def _write(
        self,
        record: dict[str, Any],
        *,
        stack: list[str],
        records: list[str],
        repos: dict[str, str],
        current: str | None = None,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{record['id']}.json").write_text(json.dumps(record, indent=1) + "\n")
        pointer = {
            "format": POINTER_FORMAT,
            "current": current or record["id"],
            "repos": repos,
            "stack": stack,
            "records": records,
            "updated_by": record["id"],
            "updated_at": record["at"],
        }
        tmp = self.pointer_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pointer, indent=1) + "\n")
        tmp.replace(self.pointer_path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def checkout(
    repos: Mapping[str, str],
    mains: Mapping[str, Path],
    siblings: Mapping[str, Path],
    workdir: Path,
    label: str,
) -> Iterator[dict[str, Path]]:
    """Detached worktrees of each repo at the pointer's commit, side by side
    (so `../clean-ai-engineering` resolves), with links to the siblings. The
    main checkouts are untouched; the worktrees are removed after the day."""
    root = workdir / f"release-{label}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    trees: dict[str, Path] = {}
    try:
        for name, sha in repos.items():
            if name not in mains:
                raise NoRelease(f"the pointer names {name!r}, which the config does not")
            trees[name] = root / name
            git(mains[name], "worktree", "add", "-q", "--detach", str(trees[name]), sha)
        for name, target in siblings.items():
            if target.exists() and name not in trees:
                (root / name).symlink_to(target)
        yield trees
    finally:
        for name, tree in trees.items():
            git(mains[name], "worktree", "remove", "--force", str(tree), check=False)
            git(mains[name], "worktree", "prune", check=False)
        shutil.rmtree(root, ignore_errors=True)


__all__ = [
    "POINTER_FORMAT",
    "RELEASE_FORMAT",
    "NoRelease",
    "Releases",
    "checkout",
    "has_commit",
    "heads",
]
