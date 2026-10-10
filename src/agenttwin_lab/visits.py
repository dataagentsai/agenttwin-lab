"""Who comes when: arrivals on the plan's curve, and the customers a calendar
event sends. Scripted and state-machine actors from AgentTwin; no model plays
a customer in the slice, so a day costs nothing and repeats exactly.
"""

from __future__ import annotations

import random
import re

from agenttwin import Live, Rule, ScriptedActor, StateMachineActor

from agenttwin_lab.plan import DayPlan, Reaction
from agenttwin_lab.ports import Visit


def arrivals(plan: DayPlan, live: Live, seed: int) -> list[Visit]:
    """`plan.visits` customers, each at a moment drawn from the hourly curve,
    each about one of their own orders, for a reason drawn from the plan."""
    if not plan.reasons:
        return []
    rng = random.Random(f"arrivals:{seed}")
    owners: dict[str, list[str]] = {}
    for key, row in sorted(live.rows.get("order", {}).items()):
        owners.setdefault(str(row["customer_id"]), []).append(key)
    customers = sorted(owners)
    hours = list(range(len(plan.curve)))
    hour_s = plan.length_s // len(plan.curve)
    reasons = list(plan.reasons)
    visits: list[Visit] = []
    for n in range(plan.visits):
        hour = rng.choices(hours, weights=plan.curve)[0]
        at = hour * hour_s + rng.randrange(hour_s)
        customer = rng.choice(customers)
        order = rng.choice(owners[customer])
        reason = rng.choices(reasons, weights=[r.weight for r in reasons])[0]
        visits.append(
            Visit(
                id=f"V-{n + 1:03d}",
                customer_id=customer,
                at_s=at,
                actor=ScriptedActor([s.format(order=order) for s in reason.says]),
                reason=reason.name,
                think_s=plan.think_s,
            )
        )
    return sorted(visits, key=lambda v: (v.at_s, v.id))


def reacting(
    reaction: Reaction, live: Live, keys: tuple[str, ...], now: int, seed: int, think_s: int
) -> list[Visit]:
    """The owners of the rows an event changed, writing in some hours later."""
    visits = []
    for key in keys:
        rng = random.Random(f"reaction:{seed}:{reaction.event}:{key}")
        row = live.get("order", key) or {}
        actor = StateMachineActor(
            reaction.opening.format(order=key),
            [
                Rule(when=re.compile(pattern, re.I), say=say.format(order=key), label=pattern)
                for pattern, say in reaction.rules
            ],
            max_turns=reaction.max_turns,
        )
        visits.append(
            Visit(
                id=f"R-{reaction.event}-{key}",
                customer_id=str(row.get("customer_id", "")),
                at_s=now + rng.randint(*reaction.after_s),
                actor=actor,
                reason=f"reaction:{reaction.event}",
                think_s=think_s,
            )
        )
    return visits


__all__ = ["arrivals", "reacting"]
