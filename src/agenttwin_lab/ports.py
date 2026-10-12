"""The lab's seams. Every adopted tool sits behind one of these; the day runner
knows only the protocols, and `compose.py` is the one place that names a tool.

| Port | Job | Default adapter |
|---|---|---|
| `Population` | the customers and orders a day starts with | Faker (`population.py`) |
| `Schedule` | arrivals, the calendar, the end of the day | SimPy (`schedule.py`) |
| `WallClock` | the agent process's wall clock follows the world's | time-machine (`wallclock.py`) |
| `TurnTracer` | a trace id per turn, deterministic by seed | OpenTelemetry (`tracing.py`) |
| `agenttwin.Binding` | the agent under test, in process | the reference agent's gate binding |
| `FixerAgent` | the coding agent fixing an incident | `real-claude`, `...-via-litellm`, `stub` |
| `ModelProxy` | a local endpoint serving the fixer's model | LiteLLM in front of Groq |
| `FixGates` | whether a fix is green | `incident-scenarios` or `four-gates` |
| `Approval` | a person's yes to a deploy | `local`, `github`, `auto-for-tests` |
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agenttwin import World

Rows = dict[str, tuple[dict[str, object], ...]]


class Population(Protocol):
    """Rows for the world's entities, the same rows for the same seed."""

    def generate(self, world: World, seed: int) -> Rows: ...


class Actor(Protocol):
    """A customer: what it says next, given what it last heard. `None` ends the visit.

    AgentTwin's `ScriptedActor` and `StateMachineActor` satisfy this."""

    def next(self, reply: str) -> str | None: ...


@dataclass
class Visit:
    """One customer opening the chat at a moment of the day and talking until done."""

    id: str
    customer_id: str
    at_s: int
    actor: Actor
    reason: str
    """Why this customer came: an intent from the day plan, or a calendar reaction."""
    think_s: int = 60
    """Seconds between reading a reply and typing the next message."""


Turn = Callable[[Visit, str | None, int], Awaitable[str]]
"""`(visit, said, now) -> reply`. `said` is `None` for the opening."""

Happening = Callable[[int], list[Visit]]
"""Something scheduled at a moment; it may bring customers who arrive later."""


class Schedule(Protocol):
    """A discrete-event schedule the runner fills, then runs to the end of the day."""

    def call_at(self, at_s: int, happening: Happening) -> None: ...

    def arrive(self, visit: Visit) -> None: ...

    async def run(self, *, until_s: int, turn: Turn, moved: Callable[[int], None]) -> None:
        """Run every event up to `until_s` in time order. `moved(t)` is called
        before anything happens at `t`, so the world's clock is there first."""
        ...


class WallClock(Protocol):
    """Makes `time.time()` in this process read the world's time.

    Entered for the day; `move_to` on every event."""

    def travel(self, start: int) -> AbstractContextManager[WallClock]: ...

    def move_to(self, at: int) -> None: ...


class TurnTracer(Protocol):
    """A span around each turn, so the agent's own spans join its trace."""

    def turn(self, name: str, attributes: Mapping[str, str]) -> AbstractContextManager[str]:
        """Enter a span; yields its trace id as 32 hex characters."""
        ...


@dataclass(frozen=True)
class FixerCall:
    """One bounded run of the fixer on one incident."""

    prompt: str
    cwd: Path
    add_dirs: Sequence[Path]
    settings: Path
    """The settings file holding the permission rules (`claude --settings`)."""
    max_turns: int
    max_budget_usd: float
    transcript: Path
    """Where the stream-json transcript goes, every line of it."""
    attempt: int = 1
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FixerRun:
    """What the executable did: its command line (never the prompt), how it
    ended, and where its transcript is."""

    argv: tuple[str, ...]
    exit_code: int
    transcript: Path
    stderr: str
    duration_s: float
    route: Mapping[str, object] = field(default_factory=dict)
    """What the adapter knows of this run beyond the transcript: for a proxied
    model, the proxy's port, readiness and what its log said about limits."""


@dataclass(frozen=True)
class ProxyEndpoint:
    """Where the fixer reaches its model, and the per-run token it presents."""

    base_url: str
    token: str


class ModelProxy(Protocol):
    """A local endpoint for the fixer's model, started for one run and
    stopped after it, whatever happens."""

    name: str
    last: dict[str, object]

    def describe(self) -> dict[str, object]: ...

    def serve(self, workdir: Path) -> AbstractContextManager[ProxyEndpoint]: ...


class FixerAgent(Protocol):
    """A headless coding agent: a brief in, changes in the worktree and a
    stream-json transcript out. It never commits; the runner does."""

    name: str

    def describe(self) -> dict[str, object]:
        """Adapter, executable, model and version, for the fix record."""
        ...

    def run(self, call: FixerCall) -> FixerRun: ...


@dataclass(frozen=True)
class GateResult:
    kind: str
    passed: bool
    summary: str
    detail: dict[str, object] = field(default_factory=dict)
    duration_s: float = 0.0


class FixGates(Protocol):
    """Whether the agent in a worktree is green. Run by the runner after the
    fixer's changes are committed, never by the fixer's word."""

    name: str

    def check(self, agent_worktree: Path, scenarios: Sequence[Path]) -> GateResult: ...


@dataclass(frozen=True)
class DeployCase:
    """What a person is shown before a deploy: the fix record's verdict and
    everything 3.5 found, in plain values."""

    incident: str
    fix_record: str
    release: str
    """The id the release will get if approved."""
    outcome: str
    verdict: str
    gates: str
    refusals: Mapping[str, Mapping[str, int]]
    """layer -> {stopped, passed}"""
    weakening: Mapping[str, int]
    """category -> edits labelled weakening on the branch"""
    weakening_tried: int
    honesty_flags: int
    honesty: Sequence[str]
    repos: Mapping[str, Mapping[str, str]]
    """repo -> {base, head}: what production moves from and to"""
    approval_required: Sequence[str] = ()
    """Paths a person must approve before merge (the AOAS, a new scenario)."""


@dataclass(frozen=True)
class Decision:
    """A yes or a no, and who said it, how and when."""

    approved: bool
    approver: str | None
    adapter: str
    at: str
    how: str
    test_only: bool = False
    detail: Mapping[str, object] = field(default_factory=dict)


class Approval(Protocol):
    """A person's approval of one deploy. Asked only for a deploy the lab
    would allow: a refused fix never reaches a person as a question."""

    name: str

    def ask(self, case: DeployCase) -> Decision: ...


__all__ = [
    "Actor",
    "Approval",
    "Decision",
    "DeployCase",
    "FixGates",
    "FixerAgent",
    "FixerCall",
    "FixerRun",
    "GateResult",
    "Happening",
    "ModelProxy",
    "Population",
    "ProxyEndpoint",
    "Rows",
    "Schedule",
    "Turn",
    "TurnTracer",
    "Visit",
    "WallClock",
]
