"""The fixer's brief (grant slice 3.4): an incident record turned into the
markdown prompt a headless coding agent is started with.

The brief is **data**: the template is `fixer/brief.md`, the spec context per
root cause is `fixer/incidents.yaml`, the protected paths and limits are
`fixer/fixer.yaml`, and the evidence is the incident record and the day's
ledger. This module only fills the template; it decides nothing about what the
fix should be.

    agenttwin-lab brief INC-0001 --run runs/clothing-day-seed7-k2
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import Any

import yaml
from agenttwin import load
from agenttwin.loader import resolve_spec

HERE = Path(__file__).resolve().parents[2]
FIXER = HERE / "fixer"


class NoSuchIncident(Exception):
    pass


@dataclass(frozen=True)
class Found:
    """An incident, and the day it was raised in."""

    incident: dict[str, Any]
    ledger: dict[str, Any]
    ledger_path: Path


def find(run_dir: Path, incident_id: str, *, seed: int | None = None) -> Found:
    """The incident in a run directory's ledgers: the first ledger (by seed)
    that raised it, or the one for `seed`. Incident ids are numbered per day."""
    ledgers = sorted(run_dir.glob("ledger-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    if not ledgers:
        raise NoSuchIncident(f"{run_dir} holds no day ledgers")
    for path in ledgers:
        ledger = json.loads(path.read_text())
        if seed is not None and ledger["seed"] != seed:
            continue
        for incident in ledger["incidents"]:
            if incident["id"] == incident_id:
                return Found(incident=incident, ledger=ledger, ledger_path=path)
    where = f"seed {seed} of {run_dir}" if seed is not None else str(run_dir)
    raise NoSuchIncident(f"no {incident_id} in {where}")


@dataclass(frozen=True)
class Workspace:
    """Where the fix happens. The brief names these paths to the fixer."""

    agent: str = "<reference-agent worktree>"
    spec: str = "<clean-ai-engineering worktree>"
    branch: str = "fix/<incident>-<run>"
    scenario_command: str = (
        "uv run python -m agenttwin run --binding evals.gate_binding:open_subject "
        "scenarios/incoming/<file>.yaml"
    )
    gates_command: str = "uv run <clean-ai-engineering>/tools/gates.py <reference-agent worktree>"


@dataclass(frozen=True)
class Limits:
    max_turns: int = 40
    max_budget_usd: float = 5.0
    retries: int = 1


@dataclass(frozen=True)
class Rules:
    """What the brief tells the fixer it may not touch, from `fixer.yaml`."""

    protected: tuple[Mapping[str, str], ...] = ()
    approval_required: tuple[Mapping[str, str], ...] = ()
    incoming: str = "scenarios/incoming"
    limits: Limits = field(default_factory=Limits)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


_STEP = re.compile(r"([^.\[]+)(?:\[id=([^\]]+)\])?")


def statement(aoas: Mapping[str, Any], path: str) -> Any:
    """The value at a dotted path in the AOAS; `[id=x]` picks a list item by id.
    Raises KeyError naming the path, so a context file citing a statement the
    spec no longer has fails loudly rather than briefing the fixer on nothing."""
    node: Any = aoas
    for part in _split(path):
        match = _STEP.fullmatch(part)
        if match is None:
            raise KeyError(path)
        key, item = match.groups()
        if not isinstance(node, Mapping) or key not in node:
            raise KeyError(f"{path}: no {key!r}")
        node = node[key]
        if item is not None:
            picked = [x for x in node if isinstance(x, Mapping) and x.get("id") == item]
            if not picked:
                raise KeyError(f"{path}: no item with id {item!r}")
            node = picked[0]
    return node


def _split(path: str) -> list[str]:
    """Dots split, except inside `[...]` (an id may hold a dot)."""
    parts, depth, current = [], 0, ""
    for ch in path:
        depth += ch == "["
        depth -= ch == "]"
        if ch == "." and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    return [*parts, current]


def _yaml(value: Any) -> str:
    if isinstance(value, str):
        return value
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=88).rstrip()


def _row_table(before: Mapping[str, Any]) -> str:
    record, truth = before.get("record") or {}, before.get("truth") or {}
    lines = ["| field | record | truth | |", "|---|---|---|---|"]
    for name in dict.fromkeys([*record, *truth]):
        r, t = record.get(name), truth.get(name)
        lines.append(f"| `{name}` | `{r}` | `{t}` | {'**differs**' if r != t else ''} |")
    return "\n".join(lines)


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- (none)"


def calendar_event(world_file: Path, ledger: Mapping[str, Any], event_id: str) -> str:
    """The event as declared in the world file, and what the day saw it do."""
    declared = next((e for e in load(world_file).calendar if e.id == event_id), None)
    happened = next((c for c in ledger["calendar"] if c["id"] == event_id), None)
    out: dict[str, Any] = {}
    if declared is not None:
        out["declared"] = json.loads(declared.model_dump_json(exclude_defaults=True))
    if happened is not None:
        out["fired"] = {k: happened[k] for k in ("at_iso", "by", "record_only", "keys")}
    return _yaml(out) if out else f"# {event_id}: not found in {world_file} or the ledger"


def render(
    found: Found,
    *,
    world_file: Path,
    context: Mapping[str, Any],
    rules: Rules,
    workspace: Workspace | None = None,
    template: str | None = None,
) -> str:
    """The brief for one incident, as markdown."""
    incident, ledger = found.incident, found.ledger
    ws = workspace or Workspace()
    root = incident["root"]
    known = context.get(root) or {}
    world = load(world_file)
    aoas_path = resolve_spec(world_file)
    aoas = yaml.safe_load(aoas_path.read_text())

    quoted = []
    for path in known.get("statements", ()):
        quoted.append(f"**`{path}`**\n\n```yaml\n{_yaml(statement(aoas, path))}\n```")
    statements = (
        "\n\n".join(quoted)
        if quoted
        else "No statements are recorded for this root cause yet. Read the AOAS."
    )
    gaps = (
        "\n".join(
            f"{i}. **{g['id']}**: {g['says'].strip()}" for i, g in enumerate(known["gaps"], 1)
        )
        if known.get("gaps")
        else "None recorded for this root cause."
    )
    event_id = root.removeprefix("calendar:") if root.startswith("calendar:") else None
    excerpt = incident.get("excerpt") or {}
    occurrences = [
        f"`{o['turn_id']}` at {_iso(o['at'])}, customer `{o['customer_id']}`, trace "
        f"`{o['trace_id']}`: {o['detail']}"
        for o in incident.get("occurrences", ())
    ]
    values = {
        "incident_id": incident["id"],
        "incident_format": incident.get("format", ""),
        "rule": incident["rule"],
        "root": root,
        "summary": (known.get("summary") or "").strip(),
        "agent_worktree": ws.agent,
        "spec_worktree": ws.spec,
        "branch": ws.branch,
        "day_id": ledger["id"],
        "seed": ledger["seed"],
        "detail": incident["detail"],
        "count": incident["count"],
        "customers": ", ".join(f"`{c}`" for c in incident["customers"]),
        "first_at": _iso(incident["first_at"]),
        "last_at": _iso(incident["last_at"]),
        "turn_ids": ", ".join(f"`{t}`" for t in incident["turn_ids"]),
        "trace_ids": ", ".join(f"`{t}`" for t in incident["trace_ids"]),
        "said": excerpt.get("said") or excerpt.get("call") or "",
        "reply": excerpt.get("reply") or excerpt.get("answer") or "",
        "world_before": _row_table((incident.get("world") or {}).get("before") or {}),
        "world_file": _relative(world_file),
        "calendar_event": calendar_event(world_file, ledger, event_id)
        if event_id
        else "# not a calendar root",
        "occurrences": _bullets(occurrences),
        "aoas_path": _spec_relative(aoas_path),
        "statements": statements,
        "gaps": gaps,
        "incoming_dir": rules.incoming,
        "scenario_command": ws.scenario_command,
        "gates_command": ws.gates_command,
        "protected": _bullets([f"`{p['path']}`: {p['why']}" for p in rules.protected]),
        "approval_required": _bullets(
            [f"`{p['path']}`: {p['why']}" for p in rules.approval_required]
        ),
        "max_turns": rules.limits.max_turns,
        "max_budget_usd": f"{rules.limits.max_budget_usd:.2f}",
        "retries": "One" if rules.limits.retries == 1 else str(rules.limits.retries),
    }
    del world  # loaded to fail loudly on a world the brief cannot read
    text = template if template is not None else (FIXER / "brief.md").read_text()
    return Template(text).substitute(values)


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(HERE))
    except ValueError:
        return str(path)


def _spec_relative(path: Path) -> str:
    """The AOAS path as the fixer will find it in the specifications worktree."""
    parts = path.resolve().parts
    if "clean-ai-engineering" in parts:
        return "/".join(parts[parts.index("clean-ai-engineering") + 1 :])
    return str(path)


def load_context(path: Path | None = None) -> dict[str, Any]:
    loaded = yaml.safe_load((path or FIXER / "incidents.yaml").read_text())
    return loaded or {}


__all__ = [
    "Found",
    "Limits",
    "NoSuchIncident",
    "Rules",
    "Workspace",
    "calendar_event",
    "find",
    "load_context",
    "render",
    "statement",
]
