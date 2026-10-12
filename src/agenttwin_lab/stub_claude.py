"""A stand-in for the `claude` executable, for running the fixer offline.

    python -m agenttwin_lab.stub_claude -p --output-format stream-json --verbose \\
        --max-turns 40 --settings s.json --permission-mode dontAsk ... < brief.md

It accepts the real CLI's flags by their real names and refuses any other, so
the runner's command line is checked on every offline run. It reads the prompt
from stdin, then:

1. replays the fixture's canned stream-json transcript for this attempt
   (`AGENTTWIN_STUB_FIXTURE`, `AGENTTWIN_STUB_ATTEMPT`), filling in the
   worktree paths;
2. runs the settings file's **hooks** as Claude Code would (3.5): for each
   tool call, every `PreToolUse` hook whose matcher matches gets the call as
   JSON on stdin (`session_id`, `transcript_path`, `cwd`, `permission_mode`,
   `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, the fields the
   2.1.241 bundle builds) and may deny it, by `permissionDecision: deny` or by
   exit code 2; then `PostToolUse` (or `PostToolUseFailure`) gets the result.
   With `--bare` no hook runs, as with the real CLI;
3. decides every call a hook did not deny **against the settings file's
   permission rules**: a denied one gets Claude Code's refusal text as its tool
   result and is listed in the result event's `permission_denials`;
4. **executes for real** the shell commands the fixture lists under `execute`
   (`uv run python` becomes this interpreter), so a write through the shell
   meets the read-only layer the way the real one would, and its output is the
   tool result;
5. applies the fixture's patches to the worktrees (`git apply`), which are the
   changes the run made through the file tools that were allowed.

What it cannot emulate: the hook-denial wording of the real CLI (its own words
here), the order Claude Code gives hooks and rules beyond "hooks first, a hook
`deny` stands, a hook never overrides a deny rule", and anything a model would
do differently after a refusal (the transcript is canned).

It never calls a model and never reads a key.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from agenttwin_lab.fixconfig import glob_regex

FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude (stub)", allow_abbrev=False)
    p.add_argument("-p", "--print", action="store_true", dest="print_")
    p.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--max-budget-usd", type=float)
    p.add_argument("--settings")
    p.add_argument("--permission-mode", choices=[
        "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan",
    ])  # fmt: skip
    p.add_argument("--add-dir", nargs="+", action="extend", default=[])
    p.add_argument("--no-session-persistence", action="store_true")
    p.add_argument("--bare", action="store_true")
    p.add_argument("--setting-sources")
    p.add_argument("--strict-mcp-config", action="store_true")
    p.add_argument("--include-hook-events", action="store_true")
    p.add_argument("--model")
    p.add_argument("--system-prompt")
    p.add_argument("--tools")
    return p


def setting_sources(value: str | None) -> list[str] | None:
    """The real CLI's parser: `""` is none, else user, project and local only."""
    if value is None:
        return None
    if value == "":
        return []
    out = []
    for name in (v.strip() for v in value.split(",")):
        if name not in ("user", "project", "local"):
            raise ValueError(
                f"Invalid setting source: {name}. Valid options are: user, project, local"
            )
        out.append(name)
    return out


class Hooks:
    """The settings file's command hooks, run the way Claude Code runs them."""

    def __init__(self, settings: dict[str, Any], *, enabled: bool) -> None:
        self.table = (
            (settings.get("hooks") or {}) if enabled and not settings.get("disableAllHooks") else {}
        )

    def _commands(self, event: str, tool: str) -> list[str]:
        out = []
        for group in self.table.get(event) or []:
            matcher = str(group.get("matcher") or "*")
            if matcher != "*" and not re.fullmatch(matcher, tool):
                continue
            out += [h["command"] for h in group.get("hooks") or [] if h.get("type") == "command"]
        return out

    def run(self, event: str, tool: str, payload: dict[str, Any]) -> tuple[bool, str]:
        """(denied, reason). Only PreToolUse can deny."""
        for command in self._commands(event, tool):
            done = subprocess.run(
                command, shell=True, input=json.dumps(payload), capture_output=True,
                text=True, check=False, timeout=60,
            )  # fmt: skip
            if event != "PreToolUse":
                continue
            if done.returncode == 2:
                return True, done.stderr.strip()
            if done.returncode == 0 and done.stdout.strip():
                try:
                    out = json.loads(done.stdout)
                except json.JSONDecodeError:
                    continue
                specific = out.get("hookSpecificOutput") or {}
                if specific.get("permissionDecision") == "deny":
                    return True, str(specific.get("permissionDecisionReason", ""))
        return False, ""


def execute(command: str, cwd: Path) -> tuple[str, bool]:
    """Run a canned shell command for real. `uv run python` is this interpreter."""
    real = re.sub(r"^\s*uv run python\b", shlex.quote(sys.executable), command)
    done = subprocess.run(
        ["/bin/sh", "-c", real], cwd=cwd, capture_output=True, text=True, check=False, timeout=120
    )
    return (done.stdout + done.stderr).strip(), done.returncode != 0


class Rules:
    """Claude Code's permission rules, as far as the stub needs them: path
    globs on the file tools (`//` is absolute), and command prefixes on Bash
    (`cmd:*`). Deny beats allow; with `dontAsk`, not allowed is denied."""

    def __init__(self, settings: dict[str, Any], mode: str | None) -> None:
        perms = settings.get("permissions") or {}
        self.allow = list(perms.get("allow") or [])
        self.deny = list(perms.get("deny") or [])
        self.mode = mode or perms.get("defaultMode")

    @staticmethod
    def _split(rule: str) -> tuple[str, str | None]:
        if "(" in rule and rule.endswith(")"):
            name, _, arg = rule.partition("(")
            return name, arg[:-1]
        return rule, None

    def _matches(self, rule: str, tool: str, tool_input: dict[str, Any]) -> bool:
        name, arg = self._split(rule)
        file_tool = tool in FILE_TOOLS
        if name != tool and not (name == "Edit" and file_tool):
            return False
        if arg is None:
            return True
        if tool == "Bash":
            command = str(tool_input.get("command", "")).strip()
            if arg.endswith(":*"):
                prefix = arg[:-2]
                return command == prefix or command.startswith(prefix + " ")
            return command == arg
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        glob = arg[1:] if arg.startswith("//") else arg
        return glob_regex(glob).fullmatch(path) is not None

    def denied(self, tool: str, tool_input: dict[str, Any]) -> bool:
        if any(self._matches(r, tool, tool_input) for r in self.deny):
            return True
        if self.mode == "dontAsk":
            return not any(self._matches(r, tool, tool_input) for r in self.allow)
        return False

    @staticmethod
    def message(tool: str, tool_input: dict[str, Any]) -> str:
        if tool == "Bash":
            command = tool_input.get("command", "")
            return f"Permission to use Bash with command {command} has been denied."
        return f"Permission to use {tool} has been denied."


def fill(value: Any, paths: dict[str, str]) -> Any:
    if isinstance(value, str):
        for token, path in paths.items():
            value = value.replace(token, path)
        return value
    if isinstance(value, list):
        return [fill(v, paths) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, paths) for k, v in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.print_ or args.output_format != "stream-json":
        print("the stub only plays -p --output-format stream-json", file=sys.stderr)
        return 2
    if not args.verbose:
        print(
            "Error: When using --print, --output-format=stream-json requires --verbose",
            file=sys.stderr,
        )
        return 1
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Error: no prompt on stdin", file=sys.stderr)
        return 1
    fixture = Path(os.environ["AGENTTWIN_STUB_FIXTURE"])
    attempt = int(os.environ.get("AGENTTWIN_STUB_ATTEMPT", "1"))
    manifest = yaml.safe_load((fixture / "stub.yaml").read_text())
    attempts = manifest["attempts"]
    plan = attempts[min(attempt, len(attempts)) - 1]

    cwd = Path.cwd().resolve()
    dirs = {cwd.name: cwd, **{Path(d).resolve().name: Path(d).resolve() for d in args.add_dir}}
    paths = {
        "{{AGENT}}": str(cwd),
        "{{SPEC}}": str(dirs.get("clean-ai-engineering", cwd)),
        "{{WORK}}": str(cwd.parent),
        "{{GATES}}": os.environ.get("AGENTTWIN_GATES_COMMAND", "uv run tools/gates.py ."),
    }
    try:
        setting_sources(args.setting_sources)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    settings = json.loads(Path(args.settings).read_text()) if args.settings else {}
    rules = Rules(settings, args.permission_mode)
    hooks = Hooks(settings, enabled=not args.bare)
    executed = set(plan.get("execute") or [])
    session = {
        "session_id": "stub",
        "transcript_path": "",
        "cwd": str(cwd),
        "permission_mode": args.permission_mode,
    }

    events = [
        fill(json.loads(line), paths)
        for line in (fixture / plan["transcript"]).read_text().splitlines()
        if line.strip()
    ]
    uses: dict[str, dict[str, Any]] = {}
    denials: list[dict[str, Any]] = []
    out = sys.stdout
    for event in events:
        content = (event.get("message") or {}).get("content") or []
        for block in content:
            if block.get("type") == "tool_use":
                uses[block["id"]] = block
            elif block.get("type") == "tool_result":
                use = uses.get(block.get("tool_use_id", ""))
                if use is None:
                    continue
                name, tool_input = use["name"], use["input"]
                call = {**session, "tool_name": name, "tool_input": tool_input,
                        "tool_use_id": use["id"]}  # fmt: skip
                hook_denied, reason = hooks.run(
                    "PreToolUse", name, {**call, "hook_event_name": "PreToolUse"}
                )
                if hook_denied:
                    # The stub's wording; the runner reads the hook's own log.
                    block["content"] = f"Hook PreToolUse:{name} denied this tool use: {reason}"
                    block["is_error"] = True
                    continue
                if rules.denied(name, tool_input):
                    block["content"] = rules.message(name, tool_input)
                    block["is_error"] = True
                    denials.append(
                        {"tool_name": name, "tool_use_id": use["id"], "tool_input": tool_input}
                    )
                    continue
                if name == "Bash" and use["id"] in executed:
                    block["content"], block["is_error"] = execute(
                        str(tool_input.get("command", "")), cwd
                    )
                errored = bool(block.get("is_error"))
                hooks.run(
                    "PostToolUseFailure" if errored else "PostToolUse",
                    name,
                    {**call, "hook_event_name": "PostToolUseFailure" if errored else "PostToolUse",
                     **({"error": block.get("content")} if errored
                        else {"tool_response": block.get("content")})},
                )  # fmt: skip
        if event.get("type") == "result":
            # The changes the run made through the tools that were allowed.
            failed = apply(plan.get("patches") or {}, dirs)
            event["permission_denials"] = denials
            if failed:
                event.update(subtype="error_during_execution", is_error=True)
                event["result"] = f"stub: a canned patch did not apply: {failed}"
        out.write(json.dumps(event) + "\n")
        out.flush()
    return 0


def apply(patches: dict[str, str], dirs: dict[str, Path]) -> str:
    fixture = Path(os.environ["AGENTTWIN_STUB_FIXTURE"])
    for repo, patch in patches.items():
        where = dirs.get(repo)
        if where is None:
            return f"{repo}: no such worktree among {sorted(dirs)}"
        done = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(fixture / patch)],
            cwd=where,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            return f"{repo}: {done.stderr.strip()}"
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
