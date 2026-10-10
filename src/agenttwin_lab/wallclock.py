"""time-machine behind the `WallClock` port.

The agent is handed the world's clock and reads it for everything it decides.
A few things in its process still read the wall: the in-memory checkpoint
store stamps what it writes with `time.time()` (the clock retention reads), as
does the harness's default `now`. Under `travel`, `time.time()` and `datetime`
read the world's moment; `move_to` keeps them there as the day moves.

**What it does not move**, found by the spike (README, "Time"):
`time.monotonic()` and `perf_counter()` are left alone — deadlines and
durations stay real, which is what a turn budget wants — and so is anything in
another process. The reference agent's approval and escalation waits run on
Temporal's time-skipping test server, a separate process whose clock
time-machine cannot reach. Those follow the world through the harness's own
seam instead: the waits move the test server to the world's clock before
every call (`evals.durable.OnClock`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import time_machine


@dataclass
class TimeMachineWall:
    """The default `WallClock`."""

    _traveller: Any = field(default=None)

    @contextmanager
    def travel(self, start: int) -> Iterator[TimeMachineWall]:
        with time_machine.travel(start, tick=False) as traveller:
            self._traveller = traveller
            try:
                yield self
            finally:
                self._traveller = None

    def move_to(self, at: int) -> None:
        if self._traveller is None:
            raise RuntimeError("move_to outside travel: the wall clock is not the world's")
        self._traveller.move_to(at)


__all__ = ["TimeMachineWall"]
