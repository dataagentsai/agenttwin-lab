"""The lab's parts on their own: pass^k, the schedule, the wall clock, the
population, the plan. Table-driven, fast, no agent.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from agenttwin import ScriptedActor, Subject, load

from agenttwin_lab.day import Lab, run_day
from agenttwin_lab.plan import load_plan, seconds
from agenttwin_lab.population import FakerPopulation, PopulationPlan
from agenttwin_lab.ports import Visit
from agenttwin_lab.report import pass_hat, report
from agenttwin_lab.schedule import SimPySchedule
from agenttwin_lab.tracing import OtelTurns
from agenttwin_lab.wallclock import TimeMachineWall

ROOT = Path(__file__).resolve().parents[1]
PLAN = load_plan(ROOT / "days" / "one-day.yaml")
WORLD = load(ROOT / "worlds" / "clothing-day.yaml")

# ------------------------------------------------------------------- pass^k

PASS_HAT = [
    # passed, runs, k, expected
    (3, 3, 3, 1.0),
    (2, 3, 3, 0.0),
    (2, 3, 1, 2 / 3),
    (2, 3, 2, 1 / 3),
    (0, 5, 1, 0.0),
    (5, 5, 5, 1.0),
    (4, 5, 4, 1 / 5),
]


@pytest.mark.parametrize(("passed", "runs", "k", "expected"), PASS_HAT)
def test_pass_hat_k(passed: int, runs: int, k: int, expected: float) -> None:
    assert pass_hat(passed, runs, k) == pytest.approx(expected)


@pytest.mark.parametrize(("passed", "runs", "k"), [(4, 3, 1), (1, 3, 4), (1, 3, 0)])
def test_pass_hat_k_refuses_the_impossible(passed: int, runs: int, k: int) -> None:
    with pytest.raises(ValueError):
        pass_hat(passed, runs, k)


def _ledger(seed: int, incidents: list[dict[str, Any]], planted: bool) -> dict[str, Any]:
    return {
        "id": f"w-{seed}",
        "world": "w",
        "seed": seed,
        "visits": 1,
        "turns": [],
        "calls": [],
        "effects": [],
        "model": {"calls": 0},
        "day": {"start": 0},
        "calendar": [{"id": "lie", "at": 100, "record_only": True, "keys": ["A"]}]
        if planted
        else [],
        "incidents": incidents,
    }


CAUGHT = {
    "id": "INC-0001",
    "rule": "reply-true",
    "root": "calendar:lie",
    "count": 2,
    "customers": ["C-1"],
    "first_at": 400,
}
OTHER = {**CAUGHT, "root": "order:A:status"}

REPORTS = [
    # name, days, (clean passed, detected passed)
    (
        "every lie caught, every day",
        [_ledger(1, [CAUGHT], True), _ledger(2, [CAUGHT], True)],
        (0, 2),
    ),
    ("a day that missed it", [_ledger(1, [CAUGHT], True), _ledger(2, [], True)], (1, 1)),
    ("an incident with another root is not a catch", [_ledger(1, [OTHER], True)], (0, 0)),
    ("clean days", [_ledger(1, [], False), _ledger(2, [], False), _ledger(3, [], False)], (3, 3)),
]


@pytest.mark.parametrize(("name", "days", "passed"), REPORTS, ids=[r[0] for r in REPORTS])
def test_the_report_counts_each_verdict(name: str, days, passed) -> None:
    r = report(days)
    assert (r["clean"]["passed"], r["detected"]["passed"]) == passed
    assert r["clean"]["pass^k"] == (1.0 if passed[0] == len(days) else 0.0)
    assert r["detected"]["pass^k"] == (1.0 if passed[1] == len(days) else 0.0)
    for d in r["days"]:
        for i in d["incidents"]:
            assert i["time_to_fix_s"] is None and i["time_to_deploy_s"] is None
            if i["root"] == "calendar:lie":
                assert i["time_to_detect_s"] == 300


# ----------------------------------------------------------------- schedule


async def test_the_schedule_runs_in_the_world_s_time_order() -> None:
    schedule = SimPySchedule()
    seen: list[tuple[str, int]] = []
    moved: list[int] = []

    def later(now: int) -> list[Visit]:
        seen.append(("event", now))
        return [Visit("V-late", "C-2", now + 100, ScriptedActor(["late"]), "reaction")]

    schedule.arrive(Visit("V-1", "C-1", 50, ScriptedActor(["a", "b"]), "x", think_s=30))
    schedule.call_at(60, later)

    async def turn(visit: Visit, said: str | None, now: int) -> str:
        seen.append((f"{visit.id}:{said}", now))
        return "ok"

    await schedule.run(until_s=1000, turn=turn, moved=moved.append)
    assert seen == [
        ("V-1:None", 50),
        ("V-1:a", 50),
        ("event", 60),
        ("V-1:b", 80),
        ("V-late:None", 160),
        ("V-late:late", 160),
    ]
    assert moved == sorted(moved) and moved[-1] == 1000


# --------------------------------------------------------------- wall clock

WORLD_MOMENTS = [1_793_557_800, 1_793_600_000, 1_793_644_199]


def test_the_wall_clock_follows_the_world() -> None:
    """`time.time()` in this process — what the harness's in-memory stores stamp
    with (`agent_harness.state._wall`) — reads the world's moment, and moves with it."""
    from agent_harness.state import _wall

    wall = TimeMachineWall()
    with wall.travel(WORLD_MOMENTS[0]):
        for moment in WORLD_MOMENTS:
            wall.move_to(moment)
            assert int(time.time()) == moment
            assert _wall() == moment
        started = time.monotonic()
        assert time.monotonic() >= started, "monotonic time is left real"
    assert int(time.time()) < WORLD_MOMENTS[0], "and the wall is itself again after the day"


async def test_the_agent_process_reads_the_world_s_time_every_turn() -> None:
    """A probe agent that answers with what its wall clock says."""

    @asynccontextmanager
    async def probe(live, *, wrap, clock, model):
        async def say(text, customer_id, conversation):
            return f"wall={int(time.time())} clock={clock()}", conversation

        yield Subject(say=say)

    lab = Lab(
        binding=probe,
        binding_name="probe",
        population=FakerPopulation(PLAN.population),
        schedule=SimPySchedule,
        wall=TimeMachineWall(),
        tracer=OtelTurns,
    )
    ledger = await run_day(lab, WORLD, PLAN, seed=7)
    replies = [t for t in ledger["turns"] if t["kind"] == "message"]
    assert replies
    assert all(t["reply"] == f"wall={t['at']} clock={t['at']}" for t in replies)


# --------------------------------------------------------------- population


@pytest.mark.parametrize("seed", [1, 7, 20261102])
def test_the_population_is_coherent_and_seeded(seed: int) -> None:
    from agenttwin import with_records

    population = FakerPopulation(PopulationPlan(customers=40, at_least={"out_for_delivery": 6}))
    rows = population.generate(WORLD, seed)
    assert rows == population.generate(WORLD, seed), "same seed, same rows"
    with_records(WORLD, rows)  # AgentTwin's own checks: enums, invariants, joins
    assert len(rows["customer"]) == 40
    assert sum(o["status"] == "out_for_delivery" for o in rows["order"]) >= 6


def test_a_population_plan_naming_an_unknown_state_is_refused() -> None:
    with pytest.raises(ValueError, match="lost"):
        FakerPopulation(PopulationPlan(status={"lost": 1})).generate(WORLD, 1)


@pytest.mark.parametrize(
    ("text", "expected"), [("30s", 30), ("15m", 900), ("6h", 21600), ("1d", 86400), (45, 45)]
)
def test_durations(text: str | int, expected: int) -> None:
    assert seconds(text) == expected
