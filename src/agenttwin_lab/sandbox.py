"""Clones to run a loop in, so no loop touches the owner's checkouts.

    agenttwin-lab loop ... --sandbox /tmp/lab-sandbox

The two repos the fixer changes are cloned with a bare `origin` each (so
"nothing was pushed" can be checked), the siblings are cloned plainly, and a
fixer config naming them is written beside them. The fix branches, the release
worktrees and the work directory live in the sandbox. The same shape as the
fixer tests' clones.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from agenttwin_lab.fixconfig import CONFIG


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_sandbox(base: Path, config: Path = CONFIG) -> Path:
    """Clone what `config` names under `base` (once) and return a config for
    the clones."""
    out = base / "fixer.yaml"
    if out.is_file():
        return out
    raw = yaml.safe_load(config.read_text())
    here = config.parent
    base.mkdir(parents=True, exist_ok=True)
    repos, siblings = {}, {}
    for name, rel in raw["repos"].items():
        source = (here / rel).resolve()
        bare = base / "remotes" / f"{name}.git"
        _git(base, "clone", "-q", "--bare", "--local", str(source), str(bare))
        repos[name] = base / "repos" / name
        _git(base, "clone", "-q", str(bare), str(repos[name]))
    for name, rel in (raw.get("siblings") or {}).items():
        siblings[name] = base / "siblings" / name
        _git(base, "clone", "-q", "--local", str((here / rel).resolve()), str(siblings[name]))
    raw["repos"] = {k: str(v) for k, v in repos.items()}
    raw["siblings"] = {k: str(v) for k, v in siblings.items()}
    raw["workdir"] = str(base / "work")
    raw["day"] = {k: str((here / v).resolve()) for k, v in raw["day"].items()}
    gates = raw["gates"].get("four-gates")
    if gates:
        gates["script"] = str((here / gates["script"]).resolve())
    out.write_text(yaml.safe_dump(raw, sort_keys=False))
    return out


__all__ = ["make_sandbox"]
