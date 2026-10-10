"""The fixer's configuration, `fixer/fixer.yaml`, read once into plain values.

Paths in the file are relative to it. `compose.default_fixer` binds the
adapters this names; nothing else branches on them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from agenttwin_lab.brief import FIXER, Limits, Rules

CONFIG = FIXER / "fixer.yaml"
API_VERSION = "agenttwin-lab-fixer/v0"
LAYERS = {"read_only": True, "hooks": True, "runner": True, "pre_commit": True}


class InvalidConfig(Exception):
    pass


@dataclass(frozen=True)
class PathRule:
    path: str
    """A glob relative to the work directory: `<repo>/<path>`. `*` stays in one
    directory, `**` crosses them (gitignore's rule, which Claude Code's
    permission rules follow)."""
    why: str

    def matches(self, relative: str) -> bool:
        return glob_regex(self.path).fullmatch(relative) is not None


def glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


@dataclass(frozen=True)
class FixerConfig:
    repos: Mapping[str, Path]
    primary: str
    siblings: Mapping[str, Path]
    workdir: Path
    adapter: str
    adapters: Mapping[str, Mapping[str, Any]]
    limits: Limits
    gates: str
    gate_options: Mapping[str, Mapping[str, Any]]
    world: Path
    plan: Path
    incoming: str
    protected: tuple[PathRule, ...]
    approval_required: tuple[PathRule, ...]
    shell_allow: tuple[str, ...]
    shell_deny: tuple[str, ...]
    binding: str = "evals.gate_binding:open_subject"
    pythonpath: tuple[str, ...] = ("src",)
    source: Path = field(default=CONFIG)
    layers: Mapping[str, bool] = field(default_factory=lambda: dict(LAYERS))
    """Which of 3.5's defences are on. All are, unless a test turns one off to
    show the next one catching what it would have stopped."""

    def layer(self, name: str) -> bool:
        return bool(self.layers.get(name, True))

    def rules(self) -> Rules:
        """What the brief tells the fixer."""
        return Rules(
            protected=tuple({"path": p.path, "why": p.why} for p in self.protected),
            approval_required=tuple(
                {"path": p.path, "why": p.why} for p in self.approval_required
            ),
            incoming=self.incoming.removeprefix(f"{self.primary}/"),
            limits=self.limits,
        )

    def protected_rule(self, relative: str) -> PathRule | None:
        return next((p for p in self.protected if p.matches(relative)), None)

    def approval_rule(self, relative: str) -> PathRule | None:
        return next((p for p in self.approval_required if p.matches(relative)), None)

    def with_(self, **changes: Any) -> FixerConfig:
        return replace(self, **changes)


def load_config(path: Path = CONFIG) -> FixerConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or raw.get("apiVersion") != API_VERSION:
        raise InvalidConfig(f"{path}: apiVersion must be {API_VERSION}")
    base = path.parent

    def at(p: str) -> Path:
        return (base / p).resolve()

    repos = {name: at(p) for name, p in raw["repos"].items()}
    if raw["primary"] not in repos:
        raise InvalidConfig(f"{path}: primary {raw['primary']!r} is not a repo")
    agent = raw["agent"]
    limits = raw.get("limits") or {}
    gates = raw["gates"]
    shell = raw.get("shell") or {}
    return FixerConfig(
        repos=repos,
        primary=raw["primary"],
        siblings={name: at(p) for name, p in (raw.get("siblings") or {}).items()},
        workdir=at(raw["workdir"]),
        adapter=agent["adapter"],
        adapters={k: v for k, v in agent.items() if k != "adapter"},
        limits=Limits(
            max_turns=int(limits.get("max_turns", 40)),
            max_budget_usd=float(limits.get("max_budget_usd", 5.0)),
            retries=int(limits.get("retries", 1)),
        ),
        gates=gates["adapter"],
        gate_options={k: v for k, v in gates.items() if k != "adapter"},
        world=at(raw["day"]["world"]),
        plan=at(raw["day"]["plan"]),
        incoming=raw["incoming"],
        protected=tuple(PathRule(**p) for p in raw.get("protected") or ()),
        approval_required=tuple(PathRule(**p) for p in raw.get("approval_required") or ()),
        shell_allow=tuple(shell.get("allow") or ()),
        shell_deny=tuple(shell.get("deny") or ()),
        binding=(raw.get("drive") or {}).get("binding", "evals.gate_binding:open_subject"),
        pythonpath=tuple((raw.get("drive") or {}).get("pythonpath") or ("src",)),
        source=path,
        layers=_layers(path, raw.get("layers") or {}),
    )


def _layers(path: Path, raw: Mapping[str, Any]) -> dict[str, bool]:
    unknown = set(raw) - set(LAYERS)
    if unknown:
        raise InvalidConfig(f"{path}: no layer {sorted(unknown)}; the layers are {sorted(LAYERS)}")
    return {name: bool(raw.get(name, on)) for name, on in LAYERS.items()}


__all__ = [
    "CONFIG",
    "LAYERS",
    "FixerConfig",
    "InvalidConfig",
    "PathRule",
    "glob_regex",
    "load_config",
]
