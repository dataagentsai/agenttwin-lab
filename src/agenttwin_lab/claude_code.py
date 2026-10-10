"""The fixer's executable, behind the `FixerAgent` port: Claude Code headless.

Two adapters, one command line:

- `RealClaude` runs the installed `claude` CLI. It needs `ANTHROPIC_API_KEY` in
  the environment, read from there and never from a file, and refuses to start
  without it.
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

from agenttwin_lab.ports import FixerCall, FixerRun

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


def argv(executable: Sequence[str], call: FixerCall, *, model: str | None) -> list[str]:
    """The command line, identical for both adapters. The prompt is not on it:
    it goes on stdin, so it is never in a process listing or the record's argv."""
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


def _execute(command: list[str], call: FixerCall, env: Mapping[str, str]) -> FixerRun:
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
    )


def _base_env(extra: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in (KEY, "CLAUDE_CODE_SIMPLE")}
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
    events: int = 0
    unparsed: int = 0

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
