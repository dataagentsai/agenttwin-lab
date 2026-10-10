"""The composition root: the one module that names the adopted tools.

Everything else codes to the ports. A different agent, scheduler or clock is a
different argument here, never a branch somewhere else.
"""

from __future__ import annotations

from pathlib import Path

from agenttwin import load_binding

from agenttwin_lab.day import Lab
from agenttwin_lab.plan import DayPlan
from agenttwin_lab.population import FakerPopulation
from agenttwin_lab.schedule import SimPySchedule
from agenttwin_lab.tracing import OtelTurns
from agenttwin_lab.wallclock import TimeMachineWall

HERE = Path(__file__).resolve().parents[2]
REFERENCE = HERE.parent / "reference-agent"
GATE_BINDING = "evals.gate_binding:open_subject"
"""The binding the reference agent's gates drive it through: the provider twin
reached by the agent's production adapter, Temporal's test server for the
waits, the projected world for tools. The lab drives the same thing."""

MINTED = (r"\bE-[0-9A-F]{8}\b",)
"""The reference agent's escalation references: `E-` and eight hex digits from
`uuid4`, minted inside its escalation workflow."""


def default_lab(plan: DayPlan, *, binding: str = GATE_BINDING, agent_root: Path = REFERENCE) -> Lab:
    return Lab(
        binding=load_binding(binding, root=agent_root),
        binding_name=binding,
        population=FakerPopulation(plan.population),
        schedule=SimPySchedule,
        wall=TimeMachineWall(),
        tracer=OtelTurns,
        minted=MINTED,
    )


__all__ = ["GATE_BINDING", "MINTED", "REFERENCE", "default_lab"]
