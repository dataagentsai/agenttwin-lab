"""SimPy behind the `Schedule` port: the day's events, in time order.

SimPy is synchronous and the agent is async, so the two meet at one seam: a
customer's process yields a SimPy event that stays pending while the agent
answers. The runner drains those requests between SimPy steps — an untriggered
event does not move SimPy's clock — then succeeds the event with the reply and
steps on. Same-time events run in the order they were scheduled, so a day is
deterministic for a seed.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any

import simpy

from agenttwin_lab.ports import Happening, Turn, Visit


@dataclass
class _Asked:
    visit: Visit
    said: str | None
    answered: Any  # simpy.Event


@dataclass
class SimPySchedule:
    """The default `Schedule`. One per day."""

    env: Any = field(default_factory=simpy.Environment)
    _asked: list[_Asked] = field(default_factory=list)

    def call_at(self, at_s: int, happening: Happening) -> None:
        self.env.process(self._happen(at_s, happening))

    def arrive(self, visit: Visit) -> None:
        self.env.process(self._visit(visit))

    async def run(self, *, until_s: int, turn: Turn, moved: Callable[[int], None]) -> None:
        env = self.env
        while True:
            while self._asked:
                asked = self._asked.pop(0)
                moved(int(env.now))
                reply = await turn(asked.visit, asked.said, int(env.now))
                asked.answered.succeed(reply)
            upcoming = env.peek()
            if upcoming > until_s:
                break
            moved(int(upcoming))
            env.step()
        moved(until_s)

    # -- processes

    def _happen(self, at_s: int, happening: Happening) -> Generator[Any, Any]:
        yield self.env.timeout(max(0, at_s - self.env.now))
        for visit in happening(int(self.env.now)):
            self.arrive(visit)

    def _visit(self, visit: Visit) -> Generator[Any, Any]:
        yield self.env.timeout(max(0, visit.at_s - self.env.now))
        reply = yield self._ask(visit, None)  # the chat opens
        while (said := visit.actor.next(reply)) is not None:
            reply = yield self._ask(visit, said)
            yield self.env.timeout(visit.think_s)

    def _ask(self, visit: Visit, said: str | None) -> Any:
        answered = self.env.event()
        self._asked.append(_Asked(visit, said, answered))
        return answered


__all__ = ["SimPySchedule"]
