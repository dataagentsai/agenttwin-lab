"""A local model proxy, behind the `ModelProxy` port: LiteLLM in front of Groq.

The route `real-claude-via-litellm` keeps the harness (Claude Code: its tools,
permissions, hooks, and the lab's protection layers) and swaps only the model.
Claude Code speaks Anthropic's Messages API to `ANTHROPIC_BASE_URL`; LiteLLM's
proxy serves `/v1/messages` there and translates each call to the upstream
model (`groq/openai/gpt-oss-120b`), tool calls included.

Checked against LiteLLM 1.105.0 (run with `uvx`, no Docker, 13 Oct 2026):

- `litellm --config <yaml> --host 127.0.0.1 --port N` serves `/v1/messages`
  (`proxy/_types.py`) and `/health/liveliness`.
- The proxy refuses to start with an unset or publicly known master key
  (`auth/master_key_boot_check.py`). Each run gets a fresh random one in
  `LITELLM_MASTER_KEY`, and Claude Code presents it as `ANTHROPIC_AUTH_TOKEN`.
- `additional_drop_params: [prompt_cache_key]`: the Messages-to-chat adapter
  sets `prompt_cache_key`, which Groq refuses ("property 'prompt_cache_key' is
  unsupported"); `drop_params: true` alone does not drop it.
- `include_reasoning: false`: gpt-oss returns its reasoning, LiteLLM hands it
  to Claude Code as a thinking block, Claude Code sends it back on the next
  turn, and Groq refuses `reasoning_content` on an assistant message. Asking
  Groq not to return the reasoning ends that loop.
- `router_settings.num_retries: 0`: the proxy does not retry; Claude Code does
  (its `api_retry` events land in the transcript), so every rate-limit wait is
  on the record.

The upstream key is read at run time (the environment first, else the one
`NAME=` line of a dotenv file), handed to the proxy process only, and never
written, logged or given to the fixer: Claude Code and every shell it runs see
only the per-run master key.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agenttwin_lab.ports import ProxyEndpoint

VERIFIED_ON = "1.105.0"


class ProxyUnavailable(Exception):
    """The proxy cannot start: no upstream key, or it never became healthy."""


def read_key(
    name: str, dotenv: Path | None, environ: Mapping[str, str] | None = None
) -> str | None:
    """The upstream key: `environ[name]`, else the `name=` line of `dotenv`.
    Nothing else in the file is read into the process."""
    env = os.environ if environ is None else environ
    if env.get(name):
        return env[name]
    if dotenv is None or not dotenv.is_file():
        return None
    for line in dotenv.read_text().splitlines():
        m = re.match(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$", line)
        if m:
            value = m.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value or None
    return None


def litellm_config(alias: str, upstream: str, key_env: str) -> dict[str, Any]:
    """The proxy's config: one model, named as Claude Code asks for it. The
    key is a reference (`os.environ/NAME`), never the value."""
    return {
        "model_list": [
            {
                "model_name": alias,
                "litellm_params": {
                    "model": upstream,
                    "api_key": f"os.environ/{key_env}",
                    "additional_drop_params": ["prompt_cache_key"],
                    "include_reasoning": False,
                },
            }
        ],
        "litellm_settings": {"drop_params": True},
        "router_settings": {"num_retries": 0},
    }


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


RATE_LIMIT = re.compile(r"RateLimitError|rate_limit_exceeded|\b429 Too Many Requests")
TOO_LARGE = re.compile(r"Request too large|413 Request Entity Too Large")
REQUESTED = re.compile(r"Limit (\d+), Requested (\d+)")


def read_log(text: str) -> dict[str, Any]:
    """What the proxy log says about limits: rate-limit lines, requests too
    large for the per-minute ceiling, and the sizes Groq said were asked."""
    return {
        "rate_limit_lines": len(RATE_LIMIT.findall(text)),
        "too_large_lines": len(TOO_LARGE.findall(text)),
        "requested_vs_limit": sorted({(int(a), int(b)) for a, b in REQUESTED.findall(text)}),
    }


Spawn = Callable[..., subprocess.Popen[bytes]]
Probe = Callable[[str], bool]


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            return bool(200 <= int(r.status) < 300)
    except (urllib.error.URLError, OSError):
        return False


@dataclass
class LiteLLMProxy:
    """LiteLLM's proxy, started for one fixer run and stopped after it.

    `spawn` and `probe` are seams for the tests (a fake process and health
    check); the defaults start the real thing with `uvx`."""

    alias: str
    upstream: str
    key_env: str = "GROQ_API_KEY"
    key_file: Path | None = None
    package: str = f"litellm[proxy]=={VERIFIED_ON}"
    host: str = "127.0.0.1"
    port: int | None = None
    start_timeout_s: float = 120.0
    spawn: Spawn = field(default=subprocess.Popen)
    probe: Probe = field(default=_healthy)
    name: str = "litellm"
    last: dict[str, Any] = field(default_factory=dict)
    """What the last `serve` saw: port, start time, log counts. Never the key."""

    def command(self, config: Path, port: int) -> list[str]:
        return [
            "uvx", "--from", self.package, "litellm",
            "--config", str(config), "--host", self.host, "--port", str(port),
            "--num_workers", "1",
        ]  # fmt: skip

    def describe(self) -> dict[str, object]:
        return {
            "proxy": self.name,
            "package": self.package,
            "proxy_version": self.package.rpartition("==")[2] or None,
            "upstream": self.upstream,
            "alias": self.alias,
            "key_env": self.key_env,
        }

    @contextmanager
    def serve(self, workdir: Path) -> Iterator[ProxyEndpoint]:
        key = read_key(self.key_env, self.key_file)
        if not key:
            raise ProxyUnavailable(
                f"{self.key_env} is in neither the environment nor {self.key_file}"
            )
        workdir.mkdir(parents=True, exist_ok=True)
        config = workdir / "litellm.yaml"
        config.write_text(yaml.safe_dump(litellm_config(self.alias, self.upstream, self.key_env)))
        port = self.port or free_port(self.host)
        token = "sk-" + secrets.token_hex(24)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        env.update({self.key_env: key, "LITELLM_MASTER_KEY": token,
                    "LITELLM_LOG": "ERROR", "LITELLM_TELEMETRY": "False"})  # fmt: skip
        log_path = workdir / "proxy.log"
        started = time.monotonic()
        with log_path.open("wb") as log:
            proc = self.spawn(
                self.command(config, port),
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=workdir,
                start_new_session=True,
            )
        del key, env
        base = f"http://{self.host}:{port}"
        self.last = {"port": port, "log": log_path.name, "config": config.name}
        try:
            while not self.probe(f"{base}/health/liveliness"):
                if proc.poll() is not None:
                    raise ProxyUnavailable(
                        f"the proxy exited with {proc.returncode}; see {log_path}"
                    )
                if time.monotonic() - started > self.start_timeout_s:
                    raise ProxyUnavailable(f"the proxy was not healthy in {self.start_timeout_s} s")
                time.sleep(0.5)
            self.last["ready_s"] = round(time.monotonic() - started, 3)
            yield ProxyEndpoint(base_url=base, token=token)
        finally:
            stop(proc)
            self.last["stopped"] = proc.poll() is not None
            text = log_path.read_text(errors="replace") if log_path.is_file() else ""
            self.last.update(read_log(text))


def stop(proc: subprocess.Popen[bytes], grace_s: float = 10.0) -> None:
    """Stop the proxy's whole process group: `uvx` does not pass SIGTERM on to
    the server it starts, so killing it alone leaves the server running."""
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5.0)):
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


__all__ = [
    "VERIFIED_ON",
    "LiteLLMProxy",
    "ProxyUnavailable",
    "free_port",
    "litellm_config",
    "read_key",
    "read_log",
    "stop",
]
