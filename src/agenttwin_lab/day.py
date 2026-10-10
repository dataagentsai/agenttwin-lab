"""One simulated day, unattended (grant slice 3.1–3.3).

The reference agent runs in process against an AgentTwin world for one day:
the population is generated into the world's records, customers arrive on the
plan's curve, the world's calendar fires, and AgentTwin's monitor judges every
call and every reply as it happens. The output is the **day ledger**: every
turn, call, effect, calendar event and incident, and the world at the end.

Everything this module uses is a port (`ports.py`) or AgentTwin's own API; it
names no adopted tool. `compose.py` hands it the adapters.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agenttwin import (
    Binding,
    Clock,
    Live,
    Moment,
    Monitor,
    ProviderTwin,
    World,
    default_watches,
    fire,
    with_records,
)

from agenttwin_lab.plan import DayPlan
from agenttwin_lab.ports import Population, Schedule, TurnTracer, Visit, WallClock
from agenttwin_lab.visits import arrivals, reacting

LEDGER_FORMAT = "agenttwin-lab-day/v0"


@dataclass(frozen=True)
class Lab:
    """The adapters a day runs on. Built by `compose.default_lab`."""

    binding: Binding
    binding_name: str
    population: Population
    schedule: Callable[[], Schedule]
    """A factory for a fresh `Schedule`: one per day."""
    wall: WallClock
    tracer: Callable[[int], TurnTracer]
    """A factory taking the seed: `tracer(seed) -> TurnTracer`."""
    minted: tuple[str, ...] = ()
    """Patterns for identifiers the agent mints from its own randomness (an
    escalation reference). The lab cannot seed them without reaching into the
    agent, so `canonical` reads them as placeholders when days are compared."""


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


async def run_day(lab: Lab, world: World, plan: DayPlan, *, seed: int) -> dict[str, Any]:
    """Run one day and return its ledger."""
    world = with_records(world, lab.population.generate(world, seed))
    live = Live.start(world)
    monitor = Monitor(live, watches=default_watches(owed_within_s=plan.owed_within_s))
    clock = Clock(start=plan.start, step_s=0)
    schedule = lab.schedule()
    tracer = lab.tracer(seed)
    day_id = f"{world.name}-{seed}"

    turns: list[dict[str, Any]] = []
    happened: list[dict[str, Any]] = []
    conversations: dict[str, object] = {}

    for visit in arrivals(plan, live, seed):
        schedule.arrive(visit)
    reactions = {r.event: r for r in plan.reactions}
    for event in world.calendar:

        def happening(now: int, event: Any = event) -> list[Visit]:
            keys = fire(live, event, seed=seed)
            happened.append(
                {
                    "at": plan.start + now,
                    "at_iso": iso(plan.start + now),
                    "kind": "calendar",
                    "id": event.id,
                    "by": event.by,
                    "record_only": event.record_only,
                    "keys": list(keys),
                }
            )
            reaction = reactions.get(event.id)
            return reacting(reaction, live, keys, now, seed, plan.think_s) if reaction else []

        schedule.call_at(event.at_s, happening)
    for t in range(plan.tick_every_s, plan.length_s, plan.tick_every_s):
        schedule.call_at(t, lambda now: (monitor.tick(plan.start + now), [])[1])

    # No `model:` block: the model must not be called. Every call it gets is
    # an overrun, counted in the ledger (SPEC, "an absent model block").
    twin = ProviderTwin(clock=clock)
    with lab.wall.travel(plan.start) as wall:

        def moved(t: int) -> None:
            clock.now = plan.start + t
            wall.move_to(plan.start + t)

        async with (
            twin,
            lab.binding(live, wrap=monitor.wrap, clock=clock, model=twin.endpoint) as subject,
        ):

            async def turn(visit: Visit, said: str | None, now: int) -> str:
                at = plan.start + now
                turn_id = f"{day_id}-T{len(turns) + 1:04d}"
                calls_from, effects_from = len(monitor.calls), len(live.effects)
                attributes = {"lab.turn": turn_id, "lab.customer": visit.customer_id}
                with tracer.turn("lab.turn", attributes) as trace_id:
                    monitor.begin(
                        Moment(
                            at=at, customer_id=visit.customer_id, turn_id=turn_id, trace_id=trace_id
                        ),
                        said or "",
                    )
                    if said is None:
                        reply = (
                            (await subject.opens(visit.customer_id)) or "" if subject.opens else ""
                        )
                    else:
                        reply, conversations[visit.id] = await subject.say(
                            said, visit.customer_id, conversations.get(visit.id)
                        )
                    raised = monitor.replied(reply)
                turns.append(
                    {
                        "id": turn_id,
                        "at": at,
                        "at_iso": iso(at),
                        "visit": visit.id,
                        "reason": visit.reason,
                        "customer_id": visit.customer_id,
                        "kind": "opening" if said is None else "message",
                        "said": said,
                        "reply": reply,
                        "trace_id": trace_id,
                        "calls": list(range(calls_from, len(monitor.calls))),
                        "effects": [list(e) for e in live.effects[effects_from:]],
                        "incidents": [i.id for i in raised],
                    }
                )
                return reply

            await schedule.run(until_s=plan.length_s, turn=turn, moved=moved)
            end = plan.start + plan.length_s
            monitor.tick(end)

    return {
        "format": LEDGER_FORMAT,
        "id": day_id,
        "world": world.name,
        "seed": seed,
        "binding": lab.binding_name,
        "day": {
            "start": plan.start,
            "start_iso": iso(plan.start),
            "seconds": plan.length_s,
            "end_iso": iso(plan.start + plan.length_s),
        },
        "population": {entity: len(rows) for entity, rows in sorted(world.records.items())},
        "visits": len({t["visit"] for t in turns}),
        "turns": turns,
        "calls": monitor.calls,
        "effects": [list(e) for e in live.effects],
        "calendar": happened,
        "incidents": monitor.report(),
        "model": {"calls": twin.calls, "overran": twin.overran},
        "end": {
            "world": live.snapshot(),
            "record_differs_from_truth": [
                {
                    "entity": e,
                    "key": k,
                    "field": f,
                    "record": live.get(e, k).get(f)  # type: ignore[union-attr]
                    if live.get(e, k)
                    else None,
                    "truth": v,
                    "cause": cause,
                }
                for (e, k, f), (v, cause) in sorted(live.diverged.items())
            ],
        },
    }


def canonical(ledger: dict[str, Any], minted: tuple[str, ...] = ()) -> str:
    """The ledger as text, with every identifier the agent minted replaced by
    `<minted-n>` in order of first appearance — so two days are compared on
    everything the seed decides, and the same reference used twice in one day
    still reads as the same reference."""
    text = json.dumps(ledger, sort_keys=True)
    seen: dict[str, str] = {}

    def placeholder(match: re.Match[str]) -> str:
        return seen.setdefault(match.group(0), f"<minted-{len(seen) + 1}>")

    for pattern in minted:
        text = re.sub(pattern, placeholder, text)
    return text


__all__ = ["LEDGER_FORMAT", "Lab", "canonical", "iso", "run_day"]
