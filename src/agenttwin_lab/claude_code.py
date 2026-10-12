"""The fixer's executable, behind the `FixerAgent` port: Claude Code headless.

Three adapters, one command line:

- `RealClaude` runs the installed `claude` CLI. It needs `ANTHROPIC_API_KEY` in
  the environment, read from there and never from a file, and refuses to start
  without it.
- `ClaudeViaProxy` (`real-claude-via-litellm`) runs the same CLI with the
  same flags, its model served by a local LiteLLM proxy in front of Groq's
  free tier (`model_proxy.py`); no Anthropic key. See its docstring for the
  environment it sets and the two flags it adds for a small context.
- `StubClaude` runs `python -m agenttwin_lab.stub_claude` with **the same
  arguments**. The stub parses them with the real CLI's flag names (an unknown
  flag is an error, as it is for `claude`), replays a canned stream-json
  transcript and applies a canned diff. It never sees a key.

The flags, checked against `claude --help` on Claude Code 2.1.241 (11 Oct
2026), and why each is here:

- `-p` / `--print`: headless, run once and exit; the prompt arrives on stdin.
- `--output-format stream-json`: every event, one JSON object a line.
- `--verbose`: required by the CLI with `-p` and stream-json ("When using
  --print, --output-format=stream-json requires --verbose").
- `--max-turns N`: the turn limit. **Hidden from `--help`**, but defined in
  2.1.241 ("Maximum number of agentic turns in non-interactive mode"); a run
  that hits it ends with result subtype `error_max_turns`.
- `--max-budget-usd X`: the spend limit per run (`--print` only); a run that
  hits it ends with `error_max_budget_usd`.
- `--settings <file>`: the permission rules (deny the protected paths, allow
  a short list of shell commands).
- `--permission-mode dontAsk`: nobody is there to ask; anything not allowed
  is refused.
- `--add-dir <dir>`: the specifications worktree, beside the agent's.
- `--no-session-persistence`: nothing saved to resume (`--print` only).
- `--setting-sources ""`: load **none** of the user, project and local
  settings. Read from the 2.1.241 bundle: the parser returns `[]` for the
  empty string, and `flagSettings` (our `--settings` file, with its hooks) and
  `policySettings` (managed; none on this laptop) are always added. This
  replaces 3.4's `--bare`, which skips hooks (`--help`: "skip hooks, LSP, ...
  and CLAUDE.md auto-discovery. Sets CLAUDE_CODE_SIMPLE=1").
- `--strict-mcp-config`: no MCP servers from any configuration (none given).
- `--include-hook-events`: the hooks' lifecycle events go into the stream-json
  transcript too.
- `--model <m>`: the model, from the fixer config.

What `--bare` also did, kept by other means (`ENV` below):

- `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`: no CLAUDE.md discovery (the bundle's
  `isClaudeMdLoadingDisabled` reads it). The brief is the whole context.
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`: no auto-memory (read first in the
  bundle's auto-memory check, before `CLAUDE_CODE_SIMPLE`).
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`: no update checks or telemetry.
- Auth: outside bare mode the bundle's key resolver, when non-interactive
  (`-p`) and `ANTHROPIC_API_KEY` is set, returns that key first. `RealClaude`
  refuses to start without it. What bare mode adds and this does not: bare
  never *reads* the keychain; here an OAuth login on the machine could still be
  read, though the key is used. Not exercised: no real model call is made
  until the owner's key exists.
- Not set: `CLAUDE_CODE_SIMPLE`, which is what `--bare` sets, and would skip
  the hooks again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenttwin_lab.ports import FixerCall, FixerRun, ModelProxy, ProxyEndpoint

KEY = "ANTHROPIC_API_KEY"
VERIFIED_ON = "2.1.241"
ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
"""What `--bare` gave that the hooks-on command line keeps by environment."""
WRITE_MATCHER = "Bash|Edit|Write|MultiEdit|NotebookEdit"
"""The Claude Code version the flags above were checked against."""


class FixerUnavailable(Exception):
    """The adapter cannot run: no key, no executable, no fixture."""


def argv(
    executable: Sequence[str], call: FixerCall, *, model: str | None, extra: Sequence[str] = ()
) -> list[str]:
    """The command line, identical for every adapter. The prompt is not on it:
    it goes on stdin, so it is never in a process listing or the record's argv.
    `extra` is what a route adds after the shared flags (the proxied route's
    short system prompt and tool list)."""
    out = [
        *executable,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(call.max_turns),
        "--max-budget-usd",
        f"{call.max_budget_usd:.2f}",
        "--settings",
        str(call.settings),
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--include-hook-events",
    ]
    for d in call.add_dirs:
        out += ["--add-dir", str(d)]
    if model:
        out += ["--model", model]
    out += list(extra)
    return out


def settings(
    *,
    workdir: Path,
    protected: Iterable[str],
    shell_allow: Iterable[str],
    shell_deny: Iterable[str],
    mains: Iterable[Path] = (),
    hooks: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The settings file Claude Code is given: the permission rules for the
    protected paths and the main checkouts, and the lab's hooks.

    Paths are absolute (`//` prefix, Claude Code's form for an absolute path),
    so the rules mean the same whichever directory the session starts in.
    Deny beats allow. Each protected glob is denied to `Edit` (which Claude
    Code applies to every file-editing tool) and to `Write` by name; so is
    everything in a main checkout.

    `hooks` maps `PreToolUse` and `PostToolUse` to a command: the pre hook sees
    every write-capable tool call (`WRITE_MATCHER`), the post hook every call,
    successful (`PostToolUse`) or not (`PostToolUseFailure`).
    """
    root = workdir.resolve()
    deny: list[str] = []
    for glob in protected:
        target = f"/{root}/{glob}"
        deny += [f"Edit({target})", f"Write({target})"]
    for main in mains:
        target = f"/{Path(main).resolve()}/**"
        deny += [f"Edit({target})", f"Write({target})"]
    deny += [f"Bash({c})" for c in shell_deny]
    allow = ["Read", "Glob", "Grep", "Edit", "Write", "TodoWrite"]
    allow += [f"Bash({c})" for c in shell_allow]
    out: dict[str, Any] = {
        "permissions": {
            "defaultMode": "dontAsk",
            "allow": allow,
            "deny": deny,
            "disableBypassPermissionsMode": "disable",
        }
    }
    if hooks:

        def entry(matcher: str, command: str) -> list[dict[str, Any]]:
            return [
                {
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": command, "timeout": 30}],
                }
            ]

        out["disableAllHooks"] = False
        out["hooks"] = {}
        if "PreToolUse" in hooks:
            out["hooks"]["PreToolUse"] = entry(WRITE_MATCHER, hooks["PreToolUse"])
        if "PostToolUse" in hooks:
            out["hooks"]["PostToolUse"] = entry("*", hooks["PostToolUse"])
            out["hooks"]["PostToolUseFailure"] = entry("*", hooks["PostToolUse"])
    return out


def _execute(
    command: list[str],
    call: FixerCall,
    env: Mapping[str, str],
    route: Mapping[str, object] | None = None,
) -> FixerRun:
    started = time.monotonic()
    call.transcript.parent.mkdir(parents=True, exist_ok=True)
    with call.transcript.open("w") as out:
        done = subprocess.run(
            command,
            input=call.prompt,
            stdout=out,
            stderr=subprocess.PIPE,
            text=True,
            cwd=call.cwd,
            env=dict(env),
            check=False,
        )
    return FixerRun(
        argv=tuple(command),
        exit_code=done.returncode,
        transcript=call.transcript,
        stderr=done.stderr[-4000:],
        duration_s=round(time.monotonic() - started, 3),
        route=dict(route or {}),
    )


UPSTREAM_KEYS = ("GROQ_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
"""Keys of other providers: never in the fixer's environment, where any shell
command it runs could print them."""


def _base_env(extra: Mapping[str, str]) -> dict[str, str]:
    drop = {KEY, "CLAUDE_CODE_SIMPLE", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *UPSTREAM_KEYS}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    # Frozen: nothing the fixer runs rewrites a lock file, so the diff is the
    # fixer's change and not uv's.
    env["UV_FROZEN"] = "1"
    env.update(ENV)
    env.update(extra)
    return env


@dataclass
class RealClaude:
    """Claude Code, headless. Never run without the owner's key."""

    executable: str = "claude"
    model: str | None = None
    name: str = "real-claude"

    def describe(self) -> dict[str, object]:
        try:
            version = subprocess.run(
                [self.executable, "--version"], capture_output=True, text=True, check=False
            ).stdout.strip()
        except OSError:
            version = "not installed"
        return {
            "adapter": self.name,
            "executable": self.executable,
            "model": self.model,
            "version": version,
            "flags_verified_on": VERIFIED_ON,
        }

    def run(self, call: FixerCall) -> FixerRun:
        key = os.environ.get(KEY)
        if not key:
            raise FixerUnavailable(
                f"{KEY} is not set in the environment; the real fixer waits for the owner's key "
                "(it is never read from a file)"
            )
        env = _base_env(call.env)
        env[KEY] = key
        return _execute(argv([self.executable], call, model=self.model), call, env)


@dataclass
class StubClaude:
    """A canned run: the stub executable replays `fixture`'s transcript and
    applies its diff. Same command line as the real one."""

    fixture: Path
    model: str | None = "stub"
    name: str = "stub"
    executable: tuple[str, ...] = field(
        default_factory=lambda: (sys.executable, "-m", "agenttwin_lab.stub_claude")
    )

    def describe(self) -> dict[str, object]:
        return {
            "adapter": self.name,
            "executable": " ".join(self.executable),
            "model": self.model,
            "version": "stub",
            "fixture": self.fixture.name,
            "flags_verified_on": VERIFIED_ON,
        }

    def run(self, call: FixerCall) -> FixerRun:
        if not (self.fixture / "stub.yaml").is_file():
            raise FixerUnavailable(f"no stub fixture at {self.fixture}")
        env = _base_env(call.env)
        env["AGENTTWIN_STUB_FIXTURE"] = str(self.fixture)
        env["AGENTTWIN_STUB_ATTEMPT"] = str(call.attempt)
        return _execute(argv(list(self.executable), call, model=self.model), call, env)


SMALL_SYSTEM_PROMPT = """\
You are a coding agent working headless on a brief. Act through your tools;
nobody will answer questions. Your working directory is {cwd}. Other
directories you may use: {add_dirs}. Shell commands not allowed by your
settings are refused; do not retry a refused command another way. Read only
what you need, in small ranges: your context is small. When you are done,
reply with the pull request text the brief asks for."""
"""The proxied route's system prompt. Claude Code's own is about 15,700 tokens
with every tool (measured through the proxy, 13 Oct 2026); Groq's free tier
refuses any request over 8,000 tokens a minute, so this route replaces it
(`--system-prompt`) and keeps six tools (`--tools`): about 2,240 tokens before
the brief. Claude Code's environment section (the working directory) goes with
the default prompt, so it is restated here."""

ROUTE_TOOLS = ("Read", "Edit", "Write", "Bash", "Glob", "Grep")
MODEL_ENV = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
"""Every model name Claude Code may ask for on its own (the background and
small-fast calls, subagents), mapped to the proxy's one model, so nothing
reaches for a Claude model the proxy does not serve."""


@dataclass
class ClaudeViaProxy:
    """Claude Code, headless, with its model served by a local proxy.

    The harness is the one `RealClaude` runs (the same `argv`, settings,
    hooks and environment); what changes is where the model is: Claude Code
    is pointed at the proxy (`ANTHROPIC_BASE_URL`, the per-run token as
    `ANTHROPIC_AUTH_TOKEN`), asks for `model` by its alias, and gets the
    upstream model behind it. Two flags are added for a small context
    (`--system-prompt`, `--tools`) and two limits on tool output
    (`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`).
    `ANTHROPIC_API_KEY` is never set: no Anthropic key is used or needed."""

    proxy: ModelProxy
    model: str
    provider: str
    upstream: str
    executable: str | tuple[str, ...] = "claude"
    """The CLI: `claude`, or (in the tests) the stub's command."""
    tier: str = "free"
    small_context: bool = True
    tools: tuple[str, ...] = ROUTE_TOOLS
    system_prompt: str = SMALL_SYSTEM_PROMPT
    file_read_max_tokens: int | None = 1500
    bash_max_output_chars: int | None = 6000
    name: str = "real-claude-via-litellm"

    @property
    def command(self) -> list[str]:
        return [self.executable] if isinstance(self.executable, str) else list(self.executable)

    def describe(self) -> dict[str, object]:
        try:
            version = (
                subprocess.run(
                    [*self.command, "--version"], capture_output=True, text=True, check=False
                ).stdout.strip()
                or "unknown"
            )
        except OSError:
            version = "not installed"
        return {
            "adapter": self.name,
            "executable": " ".join(self.command),
            "model": self.model,
            "provider": self.provider,
            "upstream_model": self.upstream,
            "route": f"claude-code -> {self.proxy.name} proxy -> {self.provider}",
            **self.proxy.describe(),
            "version": version,
            "flags_verified_on": VERIFIED_ON,
            "tier": self.tier,
            "spend_usd": 0.0 if self.tier == "free" else None,
            "small_context": self.small_context,
            "tools": list(self.tools) if self.small_context else "default",
        }

    def extra_args(self, call: FixerCall) -> list[str]:
        if not self.small_context:
            return []
        prompt = self.system_prompt.format(
            cwd=call.cwd, add_dirs=", ".join(map(str, call.add_dirs)) or "none"
        )
        return ["--system-prompt", prompt, "--tools", ",".join(self.tools)]

    def env(self, call: FixerCall, endpoint: ProxyEndpoint) -> dict[str, str]:
        env = _base_env(call.env)
        env["ANTHROPIC_BASE_URL"] = endpoint.base_url
        env["ANTHROPIC_AUTH_TOKEN"] = endpoint.token
        for name in MODEL_ENV:
            env[name] = self.model
        if self.file_read_max_tokens:
            env["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] = str(self.file_read_max_tokens)
        if self.bash_max_output_chars:
            env["BASH_MAX_OUTPUT_LENGTH"] = str(self.bash_max_output_chars)
        return env

    def run(self, call: FixerCall) -> FixerRun:
        from agenttwin_lab.model_proxy import ProxyUnavailable

        workdir = call.transcript.parent / f"proxy-{call.attempt}"
        command = argv(self.command, call, model=self.model, extra=self.extra_args(call))
        try:
            with self.proxy.serve(workdir) as endpoint:
                ran = _execute(command, call, self.env(call, endpoint))
        except ProxyUnavailable as exc:
            raise FixerUnavailable(str(exc)) from exc
        route = {"proxy_dir": workdir.name, **self.proxy.last}
        return FixerRun(
            argv=ran.argv,
            exit_code=ran.exit_code,
            transcript=ran.transcript,
            stderr=ran.stderr,
            duration_s=ran.duration_s,
            route=route,
        )


# --------------------------------------------------------------------------- reading a stream

FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]
    is_error: bool | None = None
    result: str = ""
    denied: bool = False

    @property
    def path(self) -> str | None:
        value = self.input.get("file_path") or self.input.get("notebook_path")
        return str(value) if value else None


@dataclass
class Stream:
    """What a stream-json transcript says happened, in order."""

    init: dict[str, Any] = field(default_factory=dict)
    tool_uses: list[ToolUse] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    retries: list[dict[str, Any]] = field(default_factory=list)
    """Claude Code's `api_retry` events: a rate limit or an overload waited out."""
    compactions: int = 0
    events: int = 0
    unparsed: int = 0

    def rate_limit(self) -> dict[str, Any]:
        """The waits, for the fix record: how many, how long, for which status."""
        by_status: dict[str, int] = {}
        for r in self.retries:
            key = str(r.get("error_status"))
            by_status[key] = by_status.get(key, 0) + 1
        return {
            "api_retries": len(self.retries),
            "retry_wait_s": round(
                sum(float(r.get("retry_delay_ms") or 0) for r in self.retries) / 1000, 3
            ),
            "by_status": by_status,
        }

    @property
    def final_text(self) -> str:
        return str(self.result.get("result") or "")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def read_stream(path: Path) -> Stream:
    """Parse a transcript line by line; a line that is not JSON is counted,
    never dropped silently."""
    stream = Stream()
    by_id: dict[str, ToolUse] = {}
    if not path.is_file():
        return stream
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            stream.unparsed += 1
            continue
        stream.events += 1
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            stream.init = event
        elif kind == "system" and event.get("subtype") == "api_retry":
            stream.retries.append(event)
        elif kind == "system" and event.get("subtype") == "compact_boundary":
            stream.compactions += 1
        elif kind in ("assistant", "user"):
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    use = ToolUse(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=dict(block.get("input") or {}),
                    )
                    by_id[use.id] = use
                    stream.tool_uses.append(use)
                elif block.get("type") == "tool_result":
                    seen = by_id.get(block.get("tool_use_id", ""))
                    if seen is not None:
                        seen.is_error = bool(block.get("is_error"))
                        seen.result = _text(block.get("content"))[:2000]
                        if seen.is_error and "has been denied" in seen.result:
                            seen.denied = True
        elif kind == "result":
            stream.result = event
    for denial in stream.result.get("permission_denials") or []:
        refused = by_id.get(str(denial.get("tool_use_id", "")))
        if refused is not None:
            refused.denied = True
    return stream


__all__ = [
    "MODEL_ENV",
    "ROUTE_TOOLS",
    "SMALL_SYSTEM_PROMPT",
    "UPSTREAM_KEYS",
    "ClaudeViaProxy",
    "ENV",
    "FILE_TOOLS",
    "KEY",
    "VERIFIED_ON",
    "FixerUnavailable",
    "RealClaude",
    "Stream",
    "StubClaude",
    "ToolUse",
    "WRITE_MATCHER",
    "argv",
    "read_stream",
    "settings",
]
