"""The lab's seams. Every adopted tool sits behind one of these; the day runner
knows only the protocols, and `compose.py` is the one place that names a tool.

| Port | Job | Default adapter |
|---|---|---|
| `Population` | the customers and orders a day starts with | Faker (`population.py`) |
| `Schedule` | arrivals, the calendar, the end of the day | SimPy (`schedule.py`) |
| `WallClock` | the agent process's wall clock follows the world's | time-machine (`wallclock.py`) |
| `TurnTracer` | a trace id per turn, deterministic by seed | OpenTelemetry (`tracing.py`) |
| `agenttwin.Binding` | the agent under test, in process | the reference agent's gate binding |
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
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


__all__ = [
    "Actor",
    "Happening",
    "Population",
    "Rows",
    "Schedule",
    "Turn",
    "TurnTracer",
    "Visit",
    "WallClock",
]
