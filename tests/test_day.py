"""One simulated day, end to end: the reference agent in process, the world
watching. Table-driven; every day here is a real day of the plan in
`days/one-day.yaml`, run through the reference agent's gate binding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agenttwin import INCIDENT_FORMAT, load

from agenttwin_lab.compose import MINTED, default_lab
from agenttwin_lab.day import LEDGER_FORMAT, canonical, run_day
from agenttwin_lab.plan import load_plan

ROOT = Path(__file__).resolve().parents[1]
PLAN = load_plan(ROOT / "days" / "one-day.yaml")
PLANTED = ROOT / "worlds" / "clothing-day.yaml"
CLEAN = ROOT / "worlds" / "clothing-day-clean.yaml"
EVENT = "carrier-marks-undelivered-parcels-delivered"

_days: dict[tuple[Path, int], dict[str, Any]] = {}


async def day(world: Path, seed: int, *, fresh: bool = False) -> dict[str, Any]:
    """A day's ledger, run once per (world, seed) unless asked fresh."""
    if fresh or (world, seed) not in _days:
        _days[(world, seed)] = await run_day(default_lab(PLAN), load(world), PLAN, seed=seed)
    return _days[(world, seed)]


@pytest.mark.parametrize("seed", [7, 8])
async def test_the_day_runs_unattended(seed: int) -> None:
    ledger = await day(PLANTED, seed)
    assert ledger["format"] == LEDGER_FORMAT
    assert ledger["visits"] >= PLAN.visits, "every planned visit came, and the reactions"
    assert all(t["reply"] for t in ledger["turns"]), "every turn was answered"
    assert ledger["model"] == {"calls": 0, "overran": 0}, "no model was called"
    assert ledger["day"]["start_iso"] == "2026-11-01T18:30:00+00:00"
    times = [t["at"] for t in ledger["turns"]]
    assert times == sorted(times), "turns in the world's time order"
    assert ledger["day"]["start"] <= times[0] and times[-1] < ledger["day"]["start"] + 86400


@pytest.mark.parametrize(("seed", "other"), [(7, 8)])
async def test_the_same_seed_gives_the_same_ledger(seed: int, other: int) -> None:
    first = await day(PLANTED, seed)
    again = await day(PLANTED, seed, fresh=True)
    assert canonical(first, MINTED) == canonical(again, MINTED)
    assert canonical(first, MINTED) != canonical(await day(PLANTED, other), MINTED)


@pytest.mark.parametrize("seed", [7, 8, 9])
async def test_a_clean_world_raises_nothing(seed: int) -> None:
    ledger = await day(CLEAN, seed)
    assert ledger["calendar"] == []
    assert ledger["incidents"] == []
    assert ledger["end"]["record_differs_from_truth"] == []


@pytest.mark.parametrize("seed", [7, 8, 9])
async def test_the_planted_world_raises_exactly_the_planted_incident(seed: int) -> None:
    ledger = await day(PLANTED, seed)
    [fired] = ledger["calendar"]
    assert fired["id"] == EVENT and len(fired["keys"]) == 3
    [incident] = ledger["incidents"]
    assert (incident["rule"], incident["root"]) == ("reply-true", f"calendar:{EVENT}")

    # The evidence a fixer starts from.
    assert incident["format"] == INCIDENT_FORMAT
    assert incident["customer_id"] and incident["turn_ids"] and incident["trace_ids"]
    assert len(incident["turn_ids"]) == len(incident["trace_ids"]) == incident["count"]
    assert all(len(t) == 32 for t in incident["trace_ids"])
    assert incident["excerpt"]["said"].startswith("where is my order")
    assert "delivered" in incident["excerpt"]["reply"]
    before = incident["world"]["before"]
    assert before["record"]["status"] == "delivered"
    assert before["truth"]["status"] == "out_for_delivery"
    assert incident["subject"]["key"] in fired["keys"]
    assert incident["first_at"] > fired["at"], "found after it was planted"

    # The turns it names are in the ledger, by the customer it names.
    turns = {t["id"]: t for t in ledger["turns"]}
    assert turns[incident["turn_ids"][0]]["customer_id"] == incident["customer_id"]


@pytest.mark.parametrize("seed", [7, 8, 9])
async def test_one_root_cause_is_one_incident(seed: int) -> None:
    """Three customers are told the lie; the carrier's scan is one cause."""
    ledger = await day(PLANTED, seed)
    [incident] = ledger["incidents"]
    owners = {
        ledger["end"]["world"]["order"][key]["customer_id"] for key in ledger["calendar"][0]["keys"]
    }
    assert owners <= set(incident["customers"]), "every customer told the lie is on it"
    assert incident["count"] >= len(owners)


@pytest.mark.parametrize("seed", [7])
async def test_the_customers_hit_by_the_incident_reach_a_person(seed: int) -> None:
    """What the AOAS does say: a parcel reported lost goes to a person
    (`lost_in_transit`, handoff). The agent does that; what it says first is
    the incident."""
    ledger = await day(PLANTED, seed)
    said = [t for t in ledger["turns"] if t["said"] and "never came" in t["said"]]
    assert len(said) == 3
    assert all("colleague" in t["reply"] and "reference" in t["reply"] for t in said)
