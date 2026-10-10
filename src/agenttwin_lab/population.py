"""Faker behind the `Population` port: a clothing store's customers and orders.

Faker (en_IN) supplies what is plausible and nobody tests — names, emails,
phone numbers, Indian addresses. What the world's rules read — each order's
status, its age since delivery, final sale — is drawn from the day plan's
weights with a seeded generator, and every row is coherent with the spec's
invariants before AgentTwin checks it again (`with_records`).

This adapter knows the clothing store's entities by name. A second agent's lab
writes its own; the port is what stays.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agenttwin import World
from faker import Faker

from agenttwin_lab.ports import Rows

DELIVERED_LIKE = ("delivered",)
"""States with an age since delivery. `returned` and `refunded` are left out of
the slice's day: a returned order owes a refund (`owed_when`), and the refund
path needs a model and an approval (README, "What the day leaves out")."""


@dataclass(frozen=True)
class PopulationPlan:
    customers: int = 60
    orders_per_customer: tuple[int, int] = (1, 3)
    status: dict[str, float] = field(
        default_factory=lambda: {
            "pending": 2,
            "confirmed": 1,
            "picked": 1,
            "shipped": 2,
            "out_for_delivery": 2,
            "delivered": 4,
            "cancelled": 1,
        }
    )
    at_least: dict[str, int] = field(default_factory=lambda: {"out_for_delivery": 5})
    total: tuple[int, int] = (499, 9999)
    final_sale: float = 0.1
    days_since_delivery: tuple[int, int] = (1, 25)
    locale: str = "en_IN"
    customer_key: str = "C-{n:05d}"
    order_key: str = "AB-{n:05d}"
    first_number: int = 20001


@dataclass(frozen=True)
class FakerPopulation:
    """The default `Population`."""

    plan: PopulationPlan = PopulationPlan()

    def generate(self, world: World, seed: int) -> Rows:
        fake = Faker(self.plan.locale)
        fake.seed_instance(seed)
        rng = random.Random(f"population:{seed}")
        plan = self.plan
        known = set(world.entities["order"].fields["status"].values)
        unknown = (set(plan.status) | set(plan.at_least)) - known
        if unknown:
            raise ValueError(f"the population plan names states the spec lacks: {sorted(unknown)}")

        customers: list[dict[str, object]] = []
        orders: list[dict[str, object]] = []
        for i in range(plan.customers):
            cid = plan.customer_key.format(n=plan.first_number + i)
            customers.append({"id": cid, "email": fake.email(), "phone": fake.phone_number()})
            for _ in range(rng.randint(*plan.orders_per_customer)):
                orders.append(self._order(fake, rng, cid, len(orders)))

        # At least so many in a state the calendar needs: the last orders drawn
        # are moved into it, so the seed still decides which ones.
        for state, wanted in plan.at_least.items():
            have = sum(1 for o in orders if o["status"] == state)
            for order in reversed(orders):
                if have >= wanted:
                    break
                if order["status"] != state:
                    order["status"] = state
                    order["days_since_delivery"] = self._age(rng, state)
                    have += 1
        return {"customer": tuple(customers), "order": tuple(orders)}

    def _order(self, fake: Faker, rng: random.Random, cid: str, n: int) -> dict[str, object]:
        plan = self.plan
        states, weights = zip(*plan.status.items(), strict=True)
        status = rng.choices(states, weights=weights)[0]
        return {
            "id": plan.order_key.format(n=plan.first_number + n),
            "customer_id": cid,
            "address": fake.address().replace("\n", ", "),
            "total": rng.randint(*plan.total),
            "status": status,
            "days_since_delivery": self._age(rng, status),
            "final_sale": rng.random() < plan.final_sale,
            "return_open": False,
        }

    def _age(self, rng: random.Random, status: str) -> int:
        return rng.randint(*self.plan.days_since_delivery) if status in DELIVERED_LIKE else 0


__all__ = ["FakerPopulation", "PopulationPlan"]
