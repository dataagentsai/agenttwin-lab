"""OpenTelemetry behind the `TurnTracer` port: one span per turn.

The agent emits its own spans through the OpenTelemetry API; opening a span
around each turn makes them children of it, so the turn's trace id is the
agent's trace id too, and an incident can name it. The ids come from a
generator seeded with the day's seed, so the same day has the same trace ids
and the ledger stays comparable run to run.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator


class SeededIds(IdGenerator):
    """Trace and span ids from a seeded generator, never zero."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(f"trace:{seed}")

    def generate_span_id(self) -> int:
        return self._rng.getrandbits(64) or 1

    def generate_trace_id(self) -> int:
        return self._rng.getrandbits(128) or 1


class OtelTurns:
    """The default `TurnTracer`: a private provider, so no global is touched."""

    def __init__(self, seed: int) -> None:
        self._tracer = TracerProvider(id_generator=SeededIds(seed)).get_tracer("agenttwin-lab")

    @contextmanager
    def turn(self, name: str, attributes: Mapping[str, str]) -> Iterator[str]:
        with self._tracer.start_as_current_span(name, attributes=dict(attributes)) as span:
            yield f"{span.get_span_context().trace_id:032x}"


__all__ = ["OtelTurns", "SeededIds"]
