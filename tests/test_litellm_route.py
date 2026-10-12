"""The `real-claude-via-litellm` route, offline.

Table-driven. No model and no proxy is ever reached: the proxy is a fake (a
sleeping process in its own session, so stopping it is real), the CLI is a
fake that writes down its command line and environment, and the key is a
made-up string. The live run is behind `AGENTTWIN_LIVE_GROQ=1`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from agenttwin_lab.claude_code import (
    KEY,
    MODEL_ENV,
    ROUTE_TOOLS,
    ClaudeViaProxy,
    FixerUnavailable,
    RealClaude,
    argv,
    read_stream,
)
from agenttwin_lab.compose import default_fixer
from agenttwin_lab.fixconfig import CONFIG, load_config
from agenttwin_lab.model_proxy import (
    LiteLLMProxy,
    ProxyUnavailable,
    litellm_config,
    read_key,
    read_log,
)
from agenttwin_lab.ports import FixerCall, ProxyEndpoint

FAKE_KEY = "gsk_test_not_a_real_key_0123456789"
ALIAS = "groq-gpt-oss-120b"
UPSTREAM = "groq/openai/gpt-oss-120b"


def call_for(tmp_path: Path, attempt: int = 1) -> FixerCall:
    (tmp_path / "s.json").write_text("{}")
    return FixerCall(
        prompt="# Fix INC-0001 (brief text)",
        cwd=tmp_path,
        add_dirs=[tmp_path / "spec"],
        settings=tmp_path / "s.json",
        max_turns=7,
        max_budget_usd=1.0,
        transcript=tmp_path / "fix" / f"attempt-{attempt}.jsonl",
        attempt=attempt,
        env={"AGENTTWIN_GATES_COMMAND": "the lab runs the gates"},
    )


@dataclass
class FakeProxy:
    """A `ModelProxy` that serves nothing and counts its starts and stops."""

    name: str = "litellm"
    started: int = 0
    stopped: int = 0
    last: dict[str, object] = field(default_factory=dict)
    fail: bool = False

    def describe(self) -> dict[str, object]:
        return {"proxy": self.name, "proxy_version": "1.105.0", "upstream": UPSTREAM}

    @contextmanager
    def serve(self, workdir: Path) -> Iterator[ProxyEndpoint]:
        if self.fail:
            raise ProxyUnavailable("no key")
        self.started += 1
        try:
            yield ProxyEndpoint(base_url="http://127.0.0.1:4999", token="sk-run-token")
        finally:
            self.stopped += 1
            self.last = {"port": 4999, "rate_limit_lines": 2, "too_large_lines": 1}


FAKE_CLI = """\
import json, os, sys
out = os.environ["FAKE_CLI_OUT"]
seen = {"argv": sys.argv[1:], "env": dict(os.environ), "stdin": sys.stdin.read()}
json.dump(seen, open(out, "w"))
for e in (
    {"type": "system", "subtype": "init", "model": sys.argv[sys.argv.index("--model") + 1],
     "session_id": "s1", "permissionMode": "dontAsk"},
    {"type": "system", "subtype": "api_retry", "attempt": 1, "retry_delay_ms": 1500,
     "error_status": 429},
    {"type": "system", "subtype": "api_retry", "attempt": 2, "retry_delay_ms": 3000,
     "error_status": 429},
    {"type": "system", "subtype": "api_retry", "attempt": 1, "retry_delay_ms": 500,
     "error_status": 529},
    {"type": "system", "subtype": "compact_boundary"},
    {"type": "result", "subtype": "success", "num_turns": 3, "total_cost_usd": 0, "result": "done",
     "terminal_reason": "completed"},
):
    print(json.dumps(e))
"""


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLI)
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("FAKE_CLI_OUT", str(seen))
    # Keys a careless adapter would pass on: none may reach the fixer.
    monkeypatch.setenv(KEY, "sk-ant-should-never-be-seen")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    return {"executable": (sys.executable, str(script)), "seen": seen}


def adapter(proxy: Any, executable: Any = "claude", **kw: Any) -> ClaudeViaProxy:
    return ClaudeViaProxy(
        proxy=proxy, model=ALIAS, provider="groq", upstream=UPSTREAM, executable=executable, **kw
    )


# --------------------------------------------------------------------------- the command line

# (row, small_context, present, absent)
ARGV = [
    ("small context: short prompt and six tools", True,
     ["--system-prompt", "--tools", "Read,Edit,Write,Bash,Glob,Grep", "--model", ALIAS],
     ["--bare"]),
    ("room to spare: Claude Code's own prompt and tools", False,
     ["--model", ALIAS], ["--system-prompt", "--tools", "--bare"]),
]  # fmt: skip


@pytest.mark.parametrize(("row", "small", "present", "absent"), ARGV, ids=[a[0] for a in ARGV])
def test_the_command_line_is_the_harness_plus_the_route(
    tmp_path: Path, row: str, small: bool, present: list[str], absent: list[str]
) -> None:
    call = call_for(tmp_path)
    via = adapter(FakeProxy(), small_context=small)
    command = argv(via.command, call, model=via.model, extra=via.extra_args(call))
    shared = argv(["claude"], call, model=via.model)
    assert command[: len(shared)] == shared, "the same flags as real-claude, in the same order"
    for flag in present:
        assert flag in command
    for flag in absent:
        assert flag not in command
    assert "# Fix INC-0001" not in " ".join(command), "the prompt goes on stdin"
    if small:
        prompt = command[command.index("--system-prompt") + 1]
        assert str(call.cwd) in prompt and str(call.add_dirs[0]) in prompt


# --------------------------------------------------------------------------- the environment

# (row, variable, expected value; None means absent)
ENV = [
    ("the proxy's address", "ANTHROPIC_BASE_URL", "http://127.0.0.1:4999"),
    ("the per-run token", "ANTHROPIC_AUTH_TOKEN", "sk-run-token"),
    *[(f"{name} is the alias", name, ALIAS) for name in MODEL_ENV],
    ("no Anthropic key", KEY, None),
    ("no Groq key: the fixer's shells never see it", "GROQ_API_KEY", None),
    ("file reads are capped", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS", "1500"),
    ("shell output is capped", "BASH_MAX_OUTPUT_LENGTH", "6000"),
    ("no CLAUDE.md", "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "1"),
    ("hooks stay on: not simple mode", "CLAUDE_CODE_SIMPLE", None),
    ("the runner's own variables pass", "AGENTTWIN_GATES_COMMAND", "the lab runs the gates"),
    ("uv does not rewrite the lock", "UV_FROZEN", "1"),
]


@pytest.mark.parametrize(("row", "name", "value"), ENV, ids=[e[0] for e in ENV])
def test_the_cli_is_pointed_at_the_proxy_and_sees_no_key(
    tmp_path: Path, fake_cli: dict[str, Any], row: str, name: str, value: str | None
) -> None:
    proxy = FakeProxy()
    adapter(proxy, fake_cli["executable"]).run(call_for(tmp_path))
    env = json.loads(fake_cli["seen"].read_text())["env"]
    assert env.get(name) == value
    assert FAKE_KEY not in json.dumps(env)


def test_the_proxy_is_started_and_stopped_around_each_run(
    tmp_path: Path, fake_cli: dict[str, Any]
) -> None:
    proxy = FakeProxy()
    via = adapter(proxy, fake_cli["executable"])
    for n in (1, 2):
        ran = via.run(call_for(tmp_path, attempt=n))
        assert (proxy.started, proxy.stopped) == (n, n)
        assert ran.route == {"proxy_dir": f"proxy-{n}", "port": 4999,
                             "rate_limit_lines": 2, "too_large_lines": 1}  # fmt: skip
    seen = json.loads(fake_cli["seen"].read_text())
    assert seen["stdin"] == "# Fix INC-0001 (brief text)"


def test_no_proxy_means_the_fixer_does_not_start(tmp_path: Path) -> None:
    with pytest.raises(FixerUnavailable, match="no key"):
        adapter(FakeProxy(fail=True)).run(call_for(tmp_path))


# --------------------------------------------------------------------------- the record

# (row, field of describe(), expected)
DESCRIBE = [
    ("the adapter", "adapter", "real-claude-via-litellm"),
    ("the alias", "model", ALIAS),
    ("the provider", "provider", "groq"),
    ("the upstream model", "upstream_model", UPSTREAM),
    ("the route", "route", "claude-code -> litellm proxy -> groq"),
    ("the proxy version", "proxy_version", "1.105.0"),
    ("free tier", "tier", "free"),
    ("no spend", "spend_usd", 0.0),
    ("the tools", "tools", list(ROUTE_TOOLS)),
]


@pytest.mark.parametrize(("row", "key", "value"), DESCRIBE, ids=[d[0] for d in DESCRIBE])
def test_the_record_names_the_route_and_the_model(row: str, key: str, value: Any) -> None:
    assert adapter(FakeProxy(), executable="/no/such/claude").describe()[key] == value


def test_the_transcript_s_rate_limit_waits_are_counted(
    tmp_path: Path, fake_cli: dict[str, Any]
) -> None:
    ran = adapter(FakeProxy(), fake_cli["executable"]).run(call_for(tmp_path))
    stream = read_stream(ran.transcript)
    assert stream.rate_limit() == {
        "api_retries": 3, "retry_wait_s": 5.0, "by_status": {"429": 2, "529": 1},
    }  # fmt: skip
    assert stream.compactions == 1
    assert stream.init["model"] == ALIAS


# (row, adapter in the config, class bound)
COMPOSE = [
    ("the stub", "stub", "StubClaude"),
    ("Claude Code on Anthropic", "real-claude", "RealClaude"),
    ("Claude Code through LiteLLM to Groq", "real-claude-via-litellm", "ClaudeViaProxy"),
]


@pytest.mark.parametrize(("row", "name", "cls"), COMPOSE, ids=[c[0] for c in COMPOSE])
def test_the_config_binds_the_adapter(row: str, name: str, cls: str) -> None:
    fixer = default_fixer(load_config(CONFIG), adapter=name)
    assert type(fixer.agent).__name__ == cls


def test_the_config_s_groq_route_is_free_and_proxied() -> None:
    agent = default_fixer(load_config(CONFIG), adapter="real-claude-via-litellm").agent
    assert isinstance(agent, ClaudeViaProxy) and not isinstance(agent, RealClaude)
    assert isinstance(agent.proxy, LiteLLMProxy)
    assert agent.proxy.upstream == UPSTREAM and agent.proxy.alias == agent.model == ALIAS
    assert agent.proxy.host == "127.0.0.1", "the proxy listens on this machine only"
    assert agent.tier == "free" and agent.small_context
    assert agent.proxy.key_file is not None and agent.proxy.key_file.name == ".env"


# --------------------------------------------------------------------------- the proxy

# (row, environment, dotenv text, expected)
KEYS = [
    ("the environment wins", {"GROQ_API_KEY": "from-env"}, "GROQ_API_KEY=from-file\n", "from-env"),
    ("else the file's one line", {}, "OTHER=x\nGROQ_API_KEY=from-file\n", "from-file"),
    ("quoted", {}, 'GROQ_API_KEY="quoted"\n', "quoted"),
    ("exported", {}, "export GROQ_API_KEY=exported\n", "exported"),
    ("a longer name is not it", {}, "GROQ_API_KEY_OLD=old\n", None),
    ("empty is none", {}, "GROQ_API_KEY=\n", None),
    ("no file", {}, None, None),
]  # fmt: skip


@pytest.mark.parametrize(("row", "environ", "text", "expected"), KEYS, ids=[k[0] for k in KEYS])
def test_the_key_is_read_at_run_time(
    tmp_path: Path, row: str, environ: dict[str, str], text: str | None, expected: str | None
) -> None:
    dotenv = tmp_path / ".env"
    if text is not None:
        dotenv.write_text(text)
    assert read_key("GROQ_API_KEY", dotenv, environ) == expected


# (row, LiteLLM params path, expected)
LITELLM = [
    ("served under the alias", ("model_list", 0, "model_name"), ALIAS),
    ("the upstream", ("model_list", 0, "litellm_params", "model"), UPSTREAM),
    ("the key by reference", ("model_list", 0, "litellm_params", "api_key"),
     "os.environ/GROQ_API_KEY"),
    ("Groq refuses prompt_cache_key", ("model_list", 0, "litellm_params", "additional_drop_params"),
     ["prompt_cache_key"]),
    ("no reasoning echoed back", ("model_list", 0, "litellm_params", "include_reasoning"), False),
    ("Claude Code retries, on the record", ("router_settings", "num_retries"), 0),
]  # fmt: skip


@pytest.mark.parametrize(("row", "path", "expected"), LITELLM, ids=[x[0] for x in LITELLM])
def test_the_litellm_config(row: str, path: tuple[Any, ...], expected: Any) -> None:
    node: Any = litellm_config(ALIAS, UPSTREAM, "GROQ_API_KEY")
    for step in path:
        node = node[step]
    assert node == expected


# (row, log text, expected counts)
LOGS = [
    ("quiet", "INFO started\n",
     {"rate_limit_lines": 0, "too_large_lines": 0, "requested_vs_limit": []}),
    ("a 429", 'POST /v1/messages HTTP/1.1" 429 Too Many Requests\n',
     {"rate_limit_lines": 1, "too_large_lines": 0, "requested_vs_limit": []}),
    ("too large for the minute",
     "RateLimitError: Request too large ... (TPM): Limit 8000, Requested 15741, please\n",
     {"rate_limit_lines": 1, "too_large_lines": 1, "requested_vs_limit": [(8000, 15741)]}),
]  # fmt: skip


@pytest.mark.parametrize(("row", "text", "expected"), LOGS, ids=[x[0] for x in LOGS])
def test_the_proxy_log_is_read_for_limits(row: str, text: str, expected: dict[str, Any]) -> None:
    assert read_log(text) == expected


@dataclass
class Spawned:
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    proc: subprocess.Popen[bytes] | None = None


def fake_spawn(spawned: Spawned) -> Any:
    """Record what would start, and start a sleeper in its own session instead."""

    def spawn(command: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        spawned.command, spawned.env = command, kw["env"]
        assert kw["start_new_session"], "its own process group, so all of it can be stopped"
        spawned.proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=kw["stdout"], stderr=kw["stderr"], start_new_session=True,
        )  # fmt: skip
        return spawned.proc

    return spawn


def litellm(tmp_path: Path, spawned: Spawned, **kw: Any) -> LiteLLMProxy:
    dotenv = tmp_path / "ra.env"
    dotenv.write_text(f"OTHER=1\nGROQ_API_KEY={FAKE_KEY}\n")
    probes = iter([False, False, True])
    return LiteLLMProxy(
        alias=ALIAS, upstream=UPSTREAM, key_file=dotenv, port=4998,
        spawn=fake_spawn(spawned), probe=lambda url: next(probes, True), **kw,
    )  # fmt: skip


# (row, what holds once a run is over)
PROXY = [
    ("uvx, pinned, on localhost",
     lambda s, p, d: s.command[:4] == ["uvx", "--from", "litellm[proxy]==1.105.0", "litellm"]
     and s.command[s.command.index("--host") + 1] == "127.0.0.1"),
    ("the key reaches the proxy only", lambda s, p, d: s.env["GROQ_API_KEY"] == FAKE_KEY),
    ("a fresh master key", lambda s, p, d: s.env["LITELLM_MASTER_KEY"].startswith("sk-")
     and len(s.env["LITELLM_MASTER_KEY"]) > 20),
    ("no Anthropic variable reaches it",
     lambda s, p, d: not [k for k in s.env if k.startswith("ANTHROPIC_")]),
    ("the key is in no file it wrote",
     lambda s, p, d: all(FAKE_KEY not in f.read_text() for f in d.iterdir())),
    ("the config names the key by reference",
     lambda s, p, d: yaml.safe_load((d / "litellm.yaml").read_text())["model_list"][0]
     ["litellm_params"]["api_key"] == "os.environ/GROQ_API_KEY"),
    ("stopped, the whole group", lambda s, p, d: s.proc is not None and s.proc.poll() is not None),
    ("what it saw is kept", lambda s, p, d: p.last["port"] == 4998 and p.last["stopped"] is True),
]  # fmt: skip


@pytest.mark.parametrize(("row", "holds"), PROXY, ids=[x[0] for x in PROXY])
@pytest.mark.parametrize("ends", ["normally", "with an error"])
def test_the_proxy_starts_and_always_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: str, holds: Any, ends: str
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv(KEY, "sk-ant-should-never-be-seen")
    spawned = Spawned()
    proxy = litellm(tmp_path, spawned)
    workdir = tmp_path / "proxy-1"

    class Boom(Exception):
        pass

    try:
        with proxy.serve(workdir) as endpoint:
            assert endpoint.base_url == "http://127.0.0.1:4998"
            assert endpoint.token == spawned.env["LITELLM_MASTER_KEY"]
            assert spawned.proc is not None and spawned.proc.poll() is None, "running inside"
            if ends == "with an error":
                raise Boom
    except Boom:
        pass
    assert holds(spawned, proxy, workdir)


def test_without_a_key_the_proxy_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    spawned = Spawned()
    proxy = LiteLLMProxy(alias=ALIAS, upstream=UPSTREAM, key_file=tmp_path / "missing.env",
                         spawn=fake_spawn(spawned))  # fmt: skip
    with pytest.raises(ProxyUnavailable), proxy.serve(tmp_path / "p"):
        pass
    assert spawned.proc is None


def test_a_proxy_that_dies_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"GROQ_API_KEY={FAKE_KEY}\n")

    def spawn(command: list[str], **kw: Any) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(3)"],
            stdout=kw["stdout"], stderr=kw["stderr"], start_new_session=True,
        )  # fmt: skip

    proxy = LiteLLMProxy(alias=ALIAS, upstream=UPSTREAM, key_file=dotenv, spawn=spawn,
                         probe=lambda url: False)  # fmt: skip
    with pytest.raises(ProxyUnavailable, match="exited with 3"), proxy.serve(tmp_path / "p"):
        pass


# --------------------------------------------------------------------------- live, opt-in


LIVE = os.environ.get("AGENTTWIN_LIVE_GROQ") == "1"


@pytest.mark.skipif(not LIVE, reason="live: set AGENTTWIN_LIVE_GROQ=1 (Groq free tier, real proxy)")
def test_live_one_word_through_the_proxy(tmp_path: Path) -> None:
    """One short real session: Claude Code -> LiteLLM -> Groq, no tools used."""
    via = default_fixer(load_config(CONFIG), adapter="real-claude-via-litellm").agent
    assert isinstance(via, ClaudeViaProxy)
    call = FixerCall(
        prompt="Reply with the single word: hello", cwd=tmp_path, add_dirs=[],
        settings=tmp_path / "s.json", max_turns=2, max_budget_usd=1.0,
        transcript=tmp_path / "attempt-1.jsonl",
    )  # fmt: skip
    call.settings.write_text("{}")
    ran = via.run(call)
    stream = read_stream(ran.transcript)
    assert stream.result.get("is_error") is False, stream.result.get("result")
    assert "hello" in stream.final_text.lower()
    assert ran.route.get("stopped") is True
