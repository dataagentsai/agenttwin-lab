"""The composition root: the one module that names the adopted tools.

Everything else codes to the ports. A different agent, scheduler or clock is a
different argument here, never a branch somewhere else.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agenttwin import load_binding

from agenttwin_lab.claude_code import RealClaude, StubClaude
from agenttwin_lab.day import Lab
from agenttwin_lab.fixconfig import FixerConfig
from agenttwin_lab.fixgates import FourGates, IncidentScenarios
from agenttwin_lab.plan import DayPlan
from agenttwin_lab.population import FakerPopulation
from agenttwin_lab.ports import FixerAgent, FixGates
from agenttwin_lab.schedule import SimPySchedule
from agenttwin_lab.tracing import OtelTurns
from agenttwin_lab.wallclock import TimeMachineWall

if TYPE_CHECKING:
    from agenttwin_lab.fixer import Fixer

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


STUBS = HERE / "fixer" / "stubs"


def default_fixer(
    config: FixerConfig,
    *,
    adapter: str | None = None,
    fixture: str | None = None,
    gates: str | None = None,
) -> Fixer:
    """The fixer runner with the adapters the config (or the command line) names."""
    from agenttwin_lab.fixer import Fixer

    chosen = adapter or config.adapter
    options = config.adapters.get(chosen, {})
    agent: FixerAgent
    if chosen == "stub":
        agent = StubClaude(fixture=STUBS / (fixture or options.get("fixture", "honest")))
    elif chosen == "real-claude":
        agent = RealClaude(
            executable=options.get("executable", "claude"), model=options.get("model")
        )
    else:
        raise ValueError(f"no fixer adapter {chosen!r}: stub or real-claude")
    out = config.workdir / "gate-reports"
    out.mkdir(parents=True, exist_ok=True)
    gate_kind = gates or config.gates
    judge: FixGates
    if gate_kind == "incident-scenarios":
        judge = IncidentScenarios(config.binding, config.pythonpath, out)
    elif gate_kind == "four-gates":
        script = (config.source.parent / config.gate_options["four-gates"]["script"]).resolve()
        judge = FourGates(script, config.binding, config.pythonpath, out)
    else:
        raise ValueError(f"no gates adapter {gate_kind!r}: incident-scenarios or four-gates")
    return Fixer(config=config, agent=agent, gates=judge)


__all__ = ["GATE_BINDING", "MINTED", "REFERENCE", "STUBS", "default_fixer", "default_lab"]
