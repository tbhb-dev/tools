"""The argv of each command a run starts: ssh, tmux and the harness.

Every builder is a pure function that returns a tuple of strings. No argv holds
a credential or a Connect reference. A harness's brief goes to it on stdin, or
as the path of a file in a fixed instruction, and never as its own text. The one
exception is agy's `--print`, which takes the brief as its argument while agy
has no stdin input that `--print` can use.
"""

from __future__ import annotations

import json
import shlex

from tbhbdev.alpha.agent.decisions import policy as policy_module
from tbhbdev.alpha.agent.model import (
    NAME,
    ClaudePolicy,
    CodexMode,
    CodexPolicy,
    Host,
    Invalid,
    Policy,
    Profile,
    RunKind,
    RunRequest,
)

# The tmux socket's file name, under the sockets directory.
SOCKET = "agents"
# The first prompt of an interactive run: a pointer, never the brief itself.
POINTER = "Read the brief in {path} and carry it out."
_AGY_BUILD_NOTE = (
    "Run shell commands in {worktree} unless the task names another directory."
)
# The variables a harness gets through its own mechanism, in this order.
_HANDOFF = ("CODEX_HOME", "DOCKER_CONFIG", "GH_CONFIG_DIR", "TMPDIR", "TMPPREFIX")


def ssh(
    host: Host,
    remote: tuple[str, ...],
    *,
    connect_timeout: int,
    keepalive: int,
) -> tuple[str, ...]:
    """The driver's call to HOST, which runs REMOTE in the host's login shell.

    It never prompts, gives up connecting after CONNECT_TIMEOUT seconds and
    probes the connection every KEEPALIVE seconds.
    """
    command = shlex.join((host.shell, "-lc", shlex.join(remote)))
    return (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        f"ServerAliveInterval={keepalive}",
        host.name,
        command,
    )


def tmux(sockets_dir: str, name: str, command: tuple[str, ...]) -> tuple[str, ...]:
    """A detached session `ag-NAME` that runs COMMAND, on the sockets dir's socket.

    SOCKETS_DIR is absolute, as `policy.resolve` returns it.

    Raises:
        Invalid: NAME isn't a run name.
    """
    if not NAME.fullmatch(name):
        message = f"tmux session name {name!r} must match {NAME.pattern}"
        raise Invalid(message)
    socket = policy_module.resolve(sockets_dir, SOCKET)
    return ("tmux", "-S", socket, "new-session", "-d", "-s", f"ag-{name}", *command)


def _toml(text: str) -> str:
    """TEXT as a TOML basic string, which JSON's escapes also spell."""
    return json.dumps(text)


def _codex_sandbox(profile: Profile, detail: CodexPolicy) -> list[str]:
    if detail.mode is CodexMode.FULL_ACCESS:
        return ["-s", CodexMode.FULL_ACCESS.value]
    entries = (
        *((root, "read") for root in detail.read_roots),
        *((root, "write") for root in detail.write_roots),
        *((path, "none") for path in detail.deny),
    )
    filesystem = ", ".join(f"{_toml(path)} = {_toml(mode)}" for path, mode in entries)
    table = f"{{filesystem = {{{filesystem}}}, network = {{enabled = true}}}}"
    return [
        "-c",
        f"default_permissions={_toml(profile.value)}",
        "-c",
        f"permissions.{profile.value}={table}",
    ]


def _codex(
    request: RunRequest,
    policy: Policy,
    detail: CodexPolicy,
    worktree: str,
    brief_file: str,
) -> tuple[str, ...]:
    headless = request.kind is RunKind.EXEC
    argv = ["codex"]
    if headless:
        argv += ["exec", "--skip-git-repo-check"]
        # Builds keep their rollout, which the commit hook reads the model from.
        if policy.profile is Profile.REVIEW:
            argv.append("--ephemeral")
    argv += ["--strict-config", "-C", worktree, *_codex_sandbox(policy.profile, detail)]
    argv += [
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        f"approval_policy={_toml(detail.approval_policy)}",
        "-c",
        f"web_search={_toml(detail.web_search.value)}",
    ]
    # A codex policy's environment holds every handoff variable.
    for name in _HANDOFF:
        argv += ["-c", f"shell_environment_policy.set.{name}={_toml(policy.env[name])}"]
    argv += [
        "-m",
        request.model,
        "-c",
        f"model_reasoning_effort={_toml(request.effort)}",
    ]
    argv.append("-" if headless else POINTER.format(path=brief_file))
    return tuple(argv)


def _claude(
    request: RunRequest, policy: Policy, detail: ClaudePolicy, brief_file: str
) -> tuple[str, ...]:
    headless = request.kind is RunKind.EXEC
    mode = "auto" if policy.profile is Profile.BUILD else "dontAsk"
    settings = json.dumps(detail.settings, separators=(",", ":"), sort_keys=True)
    argv = ["claude", *(["-p"] if headless else [])]
    argv += ["--model", request.model, "--effort", request.effort]
    argv += ["--permission-mode", mode, "--settings", settings]
    if not headless:
        argv.append(POINTER.format(path=brief_file))
    return tuple(argv)


def _agy(  # noqa: PLR0913, PLR0917 -- one value per fact agy's argv depends on
    request: RunRequest,
    policy: Policy,
    worktree: str,
    brief_file: str,
    project: str | None,
    brief: str | None,
) -> tuple[str, ...]:
    if project is None:
        message = "argv agy run requires agy_project"
        raise Invalid(message)
    review = policy.profile is Profile.REVIEW
    argv = ["agy" if review else "agy-agent", "--project", project]
    argv += ["--model", request.model]
    if review:
        argv += ["--mode", "plan", "--sandbox"]
    if request.kind is RunKind.START:
        return (*argv, "--prompt-interactive", POINTER.format(path=brief_file))
    if brief is None:
        message = "argv agy exec run requires agy_brief"
        raise Invalid(message)
    if request.timeout is None:
        message = "argv agy exec run requires a timeout"
        raise Invalid(message)
    # agy's model once ran commands in a credential directory, so a build's
    # brief starts by naming the worktree.
    text = (
        brief if review else f"{_AGY_BUILD_NOTE.format(worktree=worktree)}\n\n{brief}"
    )
    return (*argv, "--print-timeout", f"{request.timeout}s", "--print", text)


def harness(  # noqa: PLR0913 -- one value per fact the harness's argv depends on
    request: RunRequest,
    policy: Policy,
    *,
    worktree: str,
    brief_file: str,
    agy_project: str | None = None,
    agy_brief: str | None = None,
) -> tuple[str, ...]:
    """The harness command of REQUEST, under POLICY, that runs in WORKTREE.

    An `exec` run reads its brief on stdin, except agy, which takes AGY_BRIEF as
    the argument of `--print`. A `start` run gets a pointer to BRIEF_FILE.
    AGY_PROJECT is the id of agy's per-run project.

    Raises:
        Invalid: POLICY was built for another harness or profile than REQUEST's,
            or an agy run lacks its project, its brief or its timeout.
    """
    if policy.harness is not request.harness or policy.profile is not request.profile:
        message = (
            "argv policy was built for another harness or profile than the request's"
        )
        raise Invalid(message)
    detail = policy.detail
    if isinstance(detail, CodexPolicy):
        return _codex(request, policy, detail, worktree, brief_file)
    if isinstance(detail, ClaudePolicy):
        return _claude(request, policy, detail, brief_file)
    return _agy(request, policy, worktree, brief_file, agy_project, agy_brief)
