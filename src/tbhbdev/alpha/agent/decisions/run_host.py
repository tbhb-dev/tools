"""Run-host decisions from gathered facts, with no I/O."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import Harness, Invalid, Policy, RunKind, RunRequest


def proxy_preflight(
    harness: Harness, opt_out: str | None, *, unsandboxed: bool
) -> tuple[str | None, bool]:
    """Return the opt-out notice and whether the host needs to probe Codex."""
    if harness is not Harness.CODEX:
        return None, False
    if opt_out is not None:
        return f"{harness} sandbox opt-out: {opt_out}", False
    return None, not unsandboxed


def check_proxy_support(*, supported: bool) -> None:
    if not supported:
        message = (
            "Codex network_proxy support required (minimum 0.164.0); "
            "upgrade or configure codex_network_proxy_opt_out"
        )
        raise Invalid(message)


def otel_config(endpoint: str) -> str:
    """A Codex config with an `[otel]` table that exports traces and metrics.

    ENDPOINT is a base URL, and each signal gets its own OTLP/HTTP path. The
    table leaves out the log exporter on purpose: Codex logs tool arguments and
    an output preview, so `log_user_prompt = false`, which guards only the
    prompt text, wouldn't keep a credential or a file out of the export.
    The table has no headers, so it holds no secret.
    """
    base = endpoint.removesuffix("/")

    def exporter(signal_path: str) -> str:
        url = json.dumps(f"{base}/v1/{signal_path}")
        return f'{{ otlp-http = {{ endpoint = {url}, protocol = "binary" }} }}'

    return (
        "[otel]\n"
        f"trace_exporter = {exporter('traces')}\n"
        f"metrics_exporter = {exporter('metrics')}\n"
        "log_user_prompt = false\n"
    )


def hook_environment(value: Policy, *, unsandboxed: bool) -> dict[str, str]:
    """Reflect the shell overrides each harness applies to its launch environment."""
    if value.harness is Harness.CODEX:
        environment = {
            key: val for key, val in value.env.items() if not key.startswith("GIT_")
        }
        return environment | {"PATH": value.tool_path, "GIT_CONFIG_COUNT": "0"}
    if value.harness is Harness.CLAUDE and not unsandboxed:
        return value.env | {"PATH": value.tool_path}
    return value.env


@dataclass(frozen=True)
class IndexWorkspace:
    worktree: str
    claude_profile: str | None
    gh_fallback: bool


def index_entry(  # noqa: PLR0913 -- one value per index input
    value: RunRequest,
    *,
    name: str,
    host: str,
    started: float,
    ended: float | None,
    rc: int | None,
    workspace: IndexWorkspace | None,
) -> dict[str, object]:
    fields = request.to_dict(value)
    return {
        key: fields[key]
        for key in (
            "v",
            "kind",
            "harness",
            "model",
            "account",
            "repo",
            "class",
            "variant",
            "reason",
            "role",
            "unsandboxed",
            "authorized_by",
        )
    } | {
        "run": name,
        "host": host,
        "worktree": workspace.worktree if workspace is not None else None,
        "started": started,
        "ended": ended,
        "rc": rc,
        "cleaned": None,
        "kept": False,
        "claude_profile": workspace.claude_profile
        if workspace is not None
        else value.claude_profile,
        "trace_id": None,
        "state": "running" if rc is None else "ended",
        "gh_fallback": workspace.gh_fallback if workspace is not None else False,
    }


def agy_project(name: str, worktree: str, grants: tuple[str, ...], timeout: int) -> str:
    return json.dumps(
        {
            "name": f"agent-{name}",
            "path": worktree,
            "permissions": list(grants),
            "timeout": timeout,
        },
    )


def host_arguments(
    harness: Harness,
    kind: RunKind,
    command: tuple[str, ...],
    executable: str,
    final_output: str,
) -> tuple[str, ...]:
    command = (executable, *command[1:])
    if kind is RunKind.EXEC and harness is Harness.CODEX:
        return (*command[:-1], "--output-last-message", final_output, command[-1])
    return command


ENV_OVERRIDE = "shell_environment_policy.set."


def argv_log_line(command: tuple[str, ...]) -> str:
    """COMMAND as one log line, without the environment values it sets.

    A Codex run's argv carries the permissions table, which is what a sandbox
    failure needs, and the brief by path or on stdin. The `-c` overrides that set
    the run's environment hold credential-adjacent values, so they stay out.
    """
    shown: list[str] = []
    previous = ""
    for word in command:
        if previous == "-c" and word.startswith(ENV_OVERRIDE):
            shown.pop()
        else:
            shown.append(word)
        previous = word
    return "argv: " + shlex.join(shown)


def resource_attributes(
    endpoint: str | None, host: str, account: str, name: str
) -> dict[str, str]:
    """OpenTelemetry resource attributes that name the host and the account.

    A run without a configured endpoint gets none, so it stays as it was.
    """
    if endpoint is None:
        return {}
    return {
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"host.name={host},tbhb.account={account},tbhb.run.id={name}"
        )
    }
