"""The composition root: the one module that names the adopted tools.

Everything else codes to the ports. A different agent, scheduler or clock is a
different argument here, never a branch somewhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenttwin import load_binding

from agenttwin_lab.approval import AutoForTests, GitHubEnvironment, LocalApproval
from agenttwin_lab.claude_code import ROUTE_TOOLS, ClaudeViaProxy, RealClaude, StubClaude
from agenttwin_lab.day import Lab
from agenttwin_lab.fixconfig import FixerConfig
from agenttwin_lab.fixgates import FourGates, IncidentScenarios
from agenttwin_lab.model_proxy import LiteLLMProxy
from agenttwin_lab.plan import DayPlan
from agenttwin_lab.population import FakerPopulation
from agenttwin_lab.ports import Approval, FixerAgent, FixGates
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
FIXERS = ("stub", "real-claude", "real-claude-via-litellm")


def via_litellm(options: Mapping[str, Any], base: Path) -> ClaudeViaProxy:
    """Claude Code with its model behind a local LiteLLM proxy (Groq first)."""
    proxy_opts = dict(options.get("proxy") or {})
    key_file = options.get("key_file")
    proxy = LiteLLMProxy(
        alias=options["model"],
        upstream=options["upstream"],
        key_env=options.get("key_env", "GROQ_API_KEY"),
        key_file=(base / key_file).resolve() if key_file else None,
        **{
            k: v
            for k, v in proxy_opts.items()
            if k in ("package", "host", "port", "start_timeout_s")
        },
    )
    limits = dict(options.get("small_context") or {})
    return ClaudeViaProxy(
        proxy=proxy,
        model=options["model"],
        provider=options.get("provider", "groq"),
        upstream=options["upstream"],
        executable=options.get("executable", "claude"),
        tier=options.get("tier", "free"),
        small_context=bool(limits.get("on", True)),
        tools=tuple(limits.get("tools") or ROUTE_TOOLS),
        file_read_max_tokens=limits.get("file_read_max_tokens", 1500),
        bash_max_output_chars=limits.get("bash_max_output_chars", 6000),
    )


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
    elif chosen == "real-claude-via-litellm":
        agent = via_litellm(options, config.source.parent)
    else:
        raise ValueError(f"no fixer adapter {chosen!r}: {', '.join(FIXERS)}")
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


APPROVALS = ("local", "github", "auto-for-tests")


def approval(name: str, *, i_am_a_test: bool = False) -> Approval:
    """The approval adapter by name. `auto-for-tests` raises `NotATest`
    outside pytest unless `i_am_a_test`."""
    if name == "local":
        return LocalApproval()
    if name == "github":
        return GitHubEnvironment()
    if name == "auto-for-tests":
        return AutoForTests.allowed(i_am_a_test)
    raise ValueError(f"no approval adapter {name!r}: {', '.join(APPROVALS)}")


__all__ = [
    "APPROVALS",
    "FIXERS",
    "GATE_BINDING",
    "MINTED",
    "REFERENCE",
    "STUBS",
    "approval",
    "default_fixer",
    "default_lab",
    "via_litellm",
]
