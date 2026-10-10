"""The day plan: when the day starts, who it holds, who comes, and how the
affected customers react to the world's calendar. Data, in `days/*.yaml`.

The world file says what happens to the world (its calendar); the day plan says
what the customers do. Actors are not in the world format yet (SPEC, "Not yet
in the format"), so they live here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agenttwin_lab.population import PopulationPlan

_DURATION = re.compile(r"^(\d+)(s|m|h|d)$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(duration: str | int) -> int:
    """`30s`, `15m`, `6h`, `1d` — or a number of seconds."""
    if isinstance(duration, int):
        return duration
    match = _DURATION.match(duration)
    if match is None:
        raise ValueError(f"{duration!r} is not a duration")
    return int(match.group(1)) * _UNITS[match.group(2)]


@dataclass(frozen=True)
class Reason:
    """Why a customer comes, and what they type."""

    name: str
    weight: float
    says: tuple[str, ...]
    """Turns in order; `{order}` is one of the customer's own orders."""


@dataclass(frozen=True)
class Reaction:
    """What the customers whose rows a calendar event changed do about it."""

    event: str
    after_s: tuple[int, int]
    opening: str
    rules: tuple[tuple[str, str], ...]
    """`(pattern in the agent's reply, what the customer says next)`, in order."""
    max_turns: int = 3


@dataclass(frozen=True)
class DayPlan:
    start: int
    """The day's first moment, epoch seconds."""
    length_s: int = 86400
    think_s: int = 60
    visits: int = 30
    curve: tuple[float, ...] = (1.0,) * 24
    """Relative arrivals per hour of the day."""
    reasons: tuple[Reason, ...] = ()
    reactions: tuple[Reaction, ...] = ()
    population: PopulationPlan = field(default_factory=PopulationPlan)
    owed_within_s: int = 4 * 3600
    tick_every_s: int = 3600


def load_plan(path: Path | str) -> DayPlan:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    day = raw.get("day", {})
    population = raw.get("population", {})
    arrivals = raw.get("arrivals", {})
    monitor = raw.get("monitor", {})
    return DayPlan(
        start=int(datetime.fromisoformat(str(day["start"])).timestamp()),
        length_s=seconds(day.get("length", "1d")),
        think_s=seconds(day.get("think", "60s")),
        visits=int(arrivals.get("visits", 30)),
        curve=tuple(float(w) for w in arrivals.get("curve", (1.0,) * 24)),
        reasons=tuple(
            Reason(name=name, weight=float(r.get("weight", 1)), says=tuple(r["says"]))
            for name, r in arrivals.get("reasons", {}).items()
        ),
        reactions=tuple(
            Reaction(
                event=event,
                after_s=(seconds(r["after"][0]), seconds(r["after"][1])),
                opening=r["opening"],
                rules=tuple((rule["when"], rule["say"]) for rule in r.get("rules", ())),
                max_turns=int(r.get("max_turns", 3)),
            )
            for event, r in raw.get("reactions", {}).items()
        ),
        population=PopulationPlan(
            customers=int(population.get("customers", 60)),
            orders_per_customer=tuple(population.get("orders_per_customer", (1, 3))),
            status={
                k: float(v) for k, v in population.get("status", PopulationPlan().status).items()
            },
            at_least={k: int(v) for k, v in population.get("at_least", {}).items()},
            total=tuple(population.get("total", (499, 9999))),
            final_sale=float(population.get("final_sale", 0.1)),
            locale=str(population.get("locale", "en_IN")),
        ),
        owed_within_s=seconds(monitor.get("owed_within", "4h")),
        tick_every_s=seconds(monitor.get("tick_every", "1h")),
    )


__all__ = ["DayPlan", "Reaction", "Reason", "load_plan", "seconds"]
