"""The hidden host command: prepare, supervise and record one harness run."""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tbhbdev import userconfig
from tbhbdev.alpha.agent import (
    admission,
    bazel_servers,
    build_info,
    codex_install,
    credentials,
    host_tools,
    ownership,
    process_group,
    push,
    render,
    runfiles,
    settings,
    worktree,
)
from tbhbdev.alpha.agent.decisions import argv, config, policy, repo, request, run_state
from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    CodexMode,
    CodexPolicy,
    Harness,
    Host,
    Invalid,
    Os,
    Policy,
    Profile,
    RunKind,
    RunPaths,
    RunRequest,
    Settings,
)
from tbhbdev.alpha.agent.ownership import RunExistsError
from tbhbdev.alpha.identity import RUN_MODEL_ENV

type BazelShutdown = dict[Path, tuple[str, tuple[int, int] | None]]


@dataclass
class Run:
    files: runfiles.RunFiles
    request: RunRequest
    settings: Settings
    host: Host
    home: Path
    tmpdir: Path
    started: float
    deadline: float
    bazel_shutdown: BazelShutdown | None = None


@dataclass(frozen=True)
class Prepared:
    paths: RunPaths
    delivery: credentials.Delivery
    auth_before: str | None = dataclasses.field(repr=False)
    bases: tuple[Path, ...] = ()


def planned_paths(run: Run) -> RunPaths:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    workspace = clone / ".worktrees" / f"agent-{run.files.name}"
    return RunPaths(
        str(run.home),
        str(workspace.resolve()),
        str(run.tmpdir),
        str(runfiles.state_home(run.home) / "tbhbdev" / "agent-codex" / run.files.name),
        str((run.home / run.settings.sandbox.audit_dir).resolve()),
        str(workspace / ".git"),
        uid=os.getuid(),
    )


# These files can alter the next trusted shell or tool invocation.
_BAZEL_SHUTDOWN_SECONDS = 60
_BAZEL_SHUTDOWN_RETRIES = 3
_BAZEL_RETRY_SECONDS = 2
_STARTUP_FILES = (
    ".zshenv",
    ".zshrc",
    ".zprofile",
    ".zlogin",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".tmux.conf",
    ".config/tmux",
    ".gitconfig",
    ".config/git",
    ".config/mise",
    ".ssh",
    ".claude",
    ".claude.json",
    ".codex",
    ".codex-recovery",
)


def protected_home_paths(home: Path, sockets_dir: str) -> tuple[Path, ...]:
    sockets = home / sockets_dir
    # HOME itself is the boundary: protecting it would refuse every workspace.
    return (
        *(home / p for p in _STARTUP_FILES),
        *(p for p in sockets.parents if p.is_relative_to(home) and p != home),
    )


def check_config(configured: Settings) -> None:
    """Check configured writes against host trust paths before a run exists."""
    home = Path.home()
    sandbox = configured.sandbox
    writes = (sandbox.audit_dir, *sandbox.write_roots, *sandbox.agy.build_write)
    protected = (
        *protected_home_paths(home, configured.sockets_dir),
        userconfig.path(),
        TAINT_DIRECTORY,
    )
    if os.environ.get("ZDOTDIR"):
        protected = (*protected, home / os.environ["ZDOTDIR"])
    policy.protect_paths(
        tuple(str((home / p).resolve()) for p in writes),
        tuple(
            dict.fromkeys(str(alias) for p in protected for alias in (p, p.resolve()))
        ),
    )


def protected_home(run: Run) -> tuple[str, ...]:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    account_git = clone / ".git"
    if account_git.is_file():
        message = "account clone must have its own git directory"
        raise Invalid(message)
    mirror = worktree.mirror_path(run.home, run.request.repo)
    paths = (
        *protected_home_paths(run.home, run.settings.sockets_dir),
        account_git,
        mirror,
        mirror.parent,
        mirror.parent.parent,
        mirror.parent.parent.parent,
        userconfig.path(),
        TAINT_DIRECTORY,
    )
    if os.environ.get("ZDOTDIR"):
        paths = (*paths, run.home / os.environ["ZDOTDIR"])
    return tuple(dict.fromkeys(str(alias) for p in paths for alias in (p, p.resolve())))


def full_denies(run: Run, paths: RunPaths) -> tuple[str, ...]:
    candidates = policy.denied_paths(run.settings, run.host, paths.home, paths.uid)
    mirror = str(worktree.mirror_path(run.home, run.request.repo))
    recovery = str(run.home / ".codex-recovery")
    return tuple(
        dict.fromkeys(
            alias
            for p in (*candidates, mirror, recovery)
            for alias in (p, os.path.realpath(p))
        )
    )


def resolved_denies(run: Run, paths: RunPaths) -> tuple[str, ...]:
    candidates = full_denies(run, paths)
    if run.host.os is Os.LINUX:
        return tuple(
            dict.fromkeys(
                os.path.realpath(p)
                for p in candidates
                if Path(os.path.realpath(p)).exists()
            )
        )
    return candidates


def executable_parents(executables: tuple[str, ...]) -> tuple[str, ...]:
    """Protect parents of non-link entries, except parents resolving to root."""
    parents: list[str] = []
    for executable in executables:
        path = Path(executable)
        parent = path.parent.resolve()
        if parent != Path("/") and str(
            parent / path.name
        ) not in host_tools.executable_links(path):
            parents.append(str(path.parent))
    return tuple(parents)


def resolved_settings(run: Run) -> Settings:
    """Run settings with the home-relative write roots made absolute."""
    sandbox = run.settings.sandbox
    return dataclasses.replace(
        run.settings,
        sandbox=dataclasses.replace(
            sandbox,
            write_roots=tuple(
                str((run.home / p).resolve()) for p in sandbox.write_roots
            ),
            agy=dataclasses.replace(
                sandbox.agy,
                build_write=tuple(
                    str((run.home / p).resolve()) for p in sandbox.agy.build_write
                ),
            ),
        ),
    )


def _audit_findings(run: Run) -> list[str]:
    record = run.files.path("would_refuse")
    if not record.is_file():
        return []
    return [json.loads(line) for line in record.read_text().split("\n") if line]


def audit_writes(
    run: Run, workspace: Path, clone: Path, write_roots: tuple[str, ...]
) -> None:
    """Record the write roots a stricter policy would refuse, and refuse none."""
    findings = dict.fromkeys(_audit_findings(run))
    for finding in worktree.validate_writes(workspace, clone, write_roots):
        if finding not in findings:
            findings[finding] = None
            run.files.write("log", f"audit: would refuse: {json.dumps(finding)}\n")
    if findings:
        run.files.write(
            "would_refuse", "".join(f"{json.dumps(line)}\n" for line in findings)
        )


def unsandboxed_policy(run: Run, paths: RunPaths) -> tuple[Policy, str]:
    value = policy.build(
        run.settings,
        run.host,
        harness=run.request.harness,
        profile=run.request.profile,
        account=run.request.account,
        unsandboxed=True,
        paths=paths,
    )
    search_path = ":".join((os.environ.get("PATH", os.defpath), value.tool_path))
    executable = host_tools.resolve(
        run.request.harness.value, search_path, follow_symlinks=False
    )
    value = dataclasses.replace(value, env=value.env | {"PATH": search_path})
    return value, executable


def checked_policy(run: Run, paths: RunPaths) -> tuple[Policy, str]:
    """Check directories before even inspecting or invoking an executable."""
    if run.request.unsandboxed:
        return unsandboxed_policy(run, paths)
    directories = host_tools.search_path(run.settings, run.host.os)
    entries = run.settings.sandbox.path
    sandbox_directories = tuple(
        str(run.home / p)
        for p in (entries.linux if run.host.os is Os.LINUX else entries.macos)
    )
    protected = (
        str(run.files.root.resolve()),
        str(Path(paths.codex_home).resolve()),
        *protected_home(run),
        *host_tools.installation(),
        *host_tools.mise_installations((*directories, *sandbox_directories)),
        *directories,
        *(str(Path(p).resolve()) for p in directories),
        *sandbox_directories,
        *(str(Path(p).resolve()) for p in sandbox_directories),
    )
    resolved = dataclasses.replace(
        paths,
        worktree=str(Path(paths.worktree).resolve()),
        tmpdir=str(Path(paths.tmpdir).resolve()),
        audit_dir=str(Path(paths.audit_dir).resolve()),
        git_common_dir=None
        if paths.git_common_dir is None
        else str(Path(paths.git_common_dir).resolve()),
    )
    check_settings = resolved_settings(run)
    policy.protect_host(
        check_settings, resolved, (*protected, *full_denies(run, resolved))
    )
    clone = Path(repo.clone_path(run.home, run.request.repo))
    write_roots = (
        resolved.codex_home,
        resolved.worktree,
        resolved.tmpdir,
        resolved.audit_dir,
        "/dev/fd",
        *check_settings.sandbox.write_roots,
        *check_settings.sandbox.agy.build_write,
    )
    audit_writes(run, Path(resolved.worktree), clone, write_roots)
    worktree.validate_hooks(Path(resolved.worktree), write_roots)
    name = (
        "agy-agent"
        if run.request.harness is Harness.AGY and run.request.profile is Profile.BUILD
        else run.request.harness.value
    )
    executable = None
    copied: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    if run.request.harness is Harness.CODEX:
        found = host_tools.resolve(name, ":".join(directories), follow_symlinks=False)
        source_paths = host_tools.executable_paths(
            found, ":".join(directories), uid=resolved.uid, write_roots=write_roots
        )
        executable, copied = codex_install.prepare(found, run.home)
        protected = (*protected, *copied)
        policy.protect_host(check_settings, resolved, copied)
    value = policy.build(
        run.settings,
        run.host,
        harness=run.request.harness,
        profile=run.request.profile,
        account=run.request.account,
        unsandboxed=run.request.unsandboxed,
        paths=resolved,
        protected=protected,
        denied=resolved_denies(run, resolved),
    )
    if executable is None:
        executable = host_tools.resolve(name, value.env["PATH"], follow_symlinks=False)
    commands = [
        host_tools.resolve(name, ":".join(directories), follow_symlinks=False)
        for name in ("git", "ssh-agent", "ssh-add", "ssh", "openssl")
    ]
    if run.host.os.value == "macos":
        commands.extend(
            host_tools.resolve(name, ":".join(directories), follow_symlinks=False)
            for name in ("sysctl", "ps", "security")
        )
    executables = (
        *host_tools.git_shim_paths(commands[0], run.host.os, resolved.uid, write_roots),
        *source_paths,
        *host_tools.executable_paths(
            executable, value.env["PATH"], uid=resolved.uid, write_roots=write_roots
        ),
        *(
            p
            for command in commands
            for p in host_tools.executable_paths(
                command,
                ":".join(directories),
                uid=resolved.uid,
                write_roots=write_roots,
            )
        ),
    )
    policy.protect_host(
        check_settings,
        resolved,
        (*executables, *copied, *executable_parents(executables)),
    )
    if isinstance(value.detail, CodexPolicy) and value.detail.mode is CodexMode.PROFILE:
        value = dataclasses.replace(
            value,
            detail=dataclasses.replace(
                value.detail,
                read_roots=(
                    *value.detail.read_roots,
                    *copied,
                ),
            ),
        )
    return value, executable


TAINT_DIRECTORY = Path("/var") / "tmp" / "tbhbdev-agent-taint"


def mark_host(run: Run) -> None:
    with ExitStack() as stack:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            TAINT_DIRECTORY.mkdir(mode=0o1777)
        except FileExistsError:
            directory = os.open(TAINT_DIRECTORY, flags)
            stack.callback(os.close, directory)
        else:
            directory = os.open(TAINT_DIRECTORY, flags)
            stack.callback(os.close, directory)
            os.fchmod(directory, stat.S_ISVTX | 0o777)
        info = os.fstat(directory)
        if (
            not stat.S_ISDIR(info.st_mode)
            or not info.st_mode & stat.S_ISVTX
            or info.st_uid not in (0, os.getuid())
        ):
            message = (
                "host marker directory must be sticky and owned by root or this account"
            )
            raise Invalid(message)
        marker = os.open(
            f"{run.request.account}.jsonl",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o644,
            dir_fd=directory,
        )
        stream = stack.enter_context(os.fdopen(marker, "ab"))
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            message = (
                "host marker must be a regular file owned by this account with one link"
            )
            raise Invalid(message)
        stream.write(
            (
                json.dumps(
                    {
                        "v": 1,
                        "run": run.files.name,
                        "account": run.request.account,
                        "authorized_by": run.request.authorized_by,
                        "at": time.time(),
                    }
                )
                + "\n"
            ).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())


def announce_access(run: Run) -> None:
    notices = []
    if run.request.profile is Profile.BUILD and run.request.tainted_host:
        notices.append("untrusted-host")
    if run.request.unsandboxed:
        notices.append(
            f"unsandboxed full access authorized_by={run.request.authorized_by}"
        )
    for notice in notices:
        print(notice, file=sys.stderr)
        run.files.write("log", notice + "\n")


def preflight(run: Run) -> None:
    announce_access(run)
    _value, executable = checked_policy(run, planned_paths(run))
    if run.request.profile is Profile.REVIEW:
        references = next(
            c for c in run.settings.credentials if c.account == run.request.account
        )
        credentials.require_reviews_app(references)
    sandbox = run.settings.sandbox
    reason = (
        sandbox.codex_network_proxy_opt_out
        if run.request.harness is Harness.CODEX
        else None
    )
    if reason is not None:
        notice = f"{run.request.harness} sandbox opt-out: {reason}"
        print(notice, file=sys.stderr)
        run.files.write("log", notice + "\n")
    if (
        run.request.harness is Harness.CODEX
        and sandbox.codex_network_proxy_opt_out is None
        and not run.request.unsandboxed
        and not host_tools.codex_proxy_supported(executable, run.tmpdir)
    ):
        message = (
            "Codex network_proxy support required (minimum 0.164.0); "
            "upgrade or configure codex_network_proxy_opt_out"
        )
        raise Invalid(message)

    if run.request.unsandboxed:
        mark_host(run)


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
        shell = {
            key: val for key, val in value.env.items() if not key.startswith("GIT_")
        }
        return shell | {"PATH": value.tool_path, "GIT_CONFIG_COUNT": "0"}
    if value.harness is Harness.CLAUDE and not unsandboxed:
        return value.env | {"PATH": value.tool_path}
    return value.env


def prepare_workspace(run: Run) -> Prepared:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    paths = planned_paths(run)
    value, _ = checked_policy(run, paths)
    workspace = worktree.create(
        clone,
        run.files.name,
        run.request,
        lambda: credentials.fetch_token(run.request, run.settings),
        hook_env=hook_environment(value, unsandboxed=run.request.unsandboxed),
    )
    with worktree.preparation_step("credential delivery"):
        delivery = credentials.deliver(run.request, run.settings, run.tmpdir)
    paths = RunPaths(
        home=str(run.home),
        worktree=str(workspace.resolve()),
        tmpdir=str(run.tmpdir),
        codex_home=paths.codex_home,
        audit_dir=policy.resolve(str(run.home), run.settings.sandbox.audit_dir),
        git_common_dir=str(workspace / ".git"),
        uid=os.getuid(),
        signing_socket=str(run.tmpdir / "ssh.sock")
        if delivery.signing is not None
        else None,
    )
    with worktree.preparation_step("audit directory"):
        runfiles.private_dir(Path(paths.audit_dir))
    before = None
    if run.request.harness is Harness.CODEX:
        with worktree.preparation_step("codex auth copy"):
            runfiles.private_dir(Path(paths.codex_home))
            source = runfiles.private_dir(run.home / ".codex") / "auth.json"
            before = credentials.auth_copy(source, Path(paths.codex_home) / "auth.json")
        if run.settings.otel_endpoint is not None:
            with worktree.preparation_step("codex config"):
                runfiles.atomic(
                    Path(paths.codex_home) / "config.toml",
                    otel_config(run.settings.otel_endpoint),
                )
    return Prepared(paths, delivery, before, bazel_bases(run.home, str(workspace)))


def index_entry(
    run: Run, prepared: Prepared | None, rc: int | None
) -> dict[str, object]:
    fields = request.to_dict(run.request)
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
        "run": run.files.name,
        "host": run.host.name,
        "worktree": prepared.paths.worktree if prepared is not None else None,
        "started": run.started,
        "ended": None if rc is None else time.time(),
        "rc": rc,
        "cleaned": None,
        "kept": False,
        "claude_profile": prepared.delivery.claude_profile
        if prepared is not None
        else run.request.claude_profile,
        "trace_id": None,
        "state": "running" if rc is None else "ended",
        "gh_fallback": prepared.delivery.gh_fallback if prepared is not None else False,
    }


def _agy_project(run: Run, prepared: Prepared, grants: AgyPolicy) -> str:
    """Keep the run's project and its grants inside its private temp directory."""
    path = run.tmpdir / "agy-project.json"
    runfiles.atomic(
        path,
        json.dumps(
            {
                "name": f"agent-{run.files.name}",
                "path": prepared.paths.worktree,
                "permissions": list(grants.grants),
                "timeout": run.settings.agy_timeout_seconds,
            }
        ),
    )
    return str(path)


def harness_command(
    run: Run, prepared: Prepared, value: Policy, executable: str
) -> tuple[str, ...]:
    paths = prepared.paths
    text = run.files.path("prompt").read_text()
    project = (
        _agy_project(run, prepared, value.detail)
        if isinstance(value.detail, AgyPolicy)
        else None
    )
    schema = run.files.path("schema.json")
    schema_text = schema.read_text() if schema.is_file() else None
    command = argv.harness(
        run.request,
        value,
        worktree=paths.worktree,
        brief_file=str(run.files.path("prompt")),
        agy_project=project,
        agy_brief=text,
        schema_file=str(schema) if schema_text is not None else None,
        schema_text=schema_text,
    )
    command = (executable, *command[1:])
    if run.request.kind is RunKind.EXEC and run.request.harness is Harness.CODEX:
        command = (
            *command[:-1],
            "--output-last-message",
            str(run.tmpdir / "out.final"),
            command[-1],
        )
    return command


def checked_harness_command(
    run: Run, prepared: Prepared, value: Policy, executable: str
) -> tuple[str, ...]:
    try:
        return harness_command(run, prepared, value, executable)
    except KeyError, TypeError:
        message = "run failed while building the harness command"
        raise Invalid(message) from None


def log_hook_tools(run: Run, paths: RunPaths) -> None:
    status = (
        "present"
        if (Path(paths.worktree) / ".bazel_env" / "bin").is_dir()
        else "missing; commit hooks that call .bazel_env/bin will fail in this run"
    )
    run.files.write("log", f"preflight: .bazel_env {status}\n")


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


def log_argv(run: Run, command: tuple[str, ...]) -> None:
    if run.request.harness is Harness.CODEX:
        # A failed log write must not stop the run, as with the cleanup notices.
        with suppress(OSError):
            run.files.write("log", argv_log_line(command) + "\n")


def resource_attributes(run: Run) -> dict[str, str]:
    """OpenTelemetry resource attributes that name the host and the account.

    A run without a configured endpoint gets none, so it stays as it was.
    """
    if run.settings.otel_endpoint is None:
        return {}
    return {
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"host.name={run.host.name},tbhb.account={run.request.account}"
        )
    }


def run_harness(
    run: Run, prepared: Prepared, stack: ExitStack, *, preflight_only: bool = False
) -> int:
    paths = prepared.paths
    value, executable = checked_policy(run, paths)
    env = (
        value.env
        | prepared.delivery.env
        | resource_attributes(run)
        | {RUN_MODEL_ENV: run.request.model}
    )
    command = checked_harness_command(run, prepared, value, executable)
    if preflight_only:
        log_hook_tools(run, paths)
        run.files.write("log", f"preflight ok: {executable}\n")
        return 0
    log_argv(run, command)
    headless = run.request.kind is RunKind.EXEC
    terminal = None if headless else sys.stdin.fileno()
    streams = (None, None, None)
    if headless:
        streams = (
            stack.enter_context(run.files.path("prompt").open()),
            stack.enter_context(run.files.path("out").open("w")),
            stack.enter_context(run.files.path("log").open("a")),
        )
    if time.monotonic() >= run.deadline:
        raise TimeoutError
    child = None

    def started(value: subprocess.Popen[str]) -> None:
        nonlocal child
        child = value
        process_group.record(run.files, child.pid)
        run.files.write("launched", "\n")

    try:
        with process_group.defer_signals():
            child = process_group.launch_owned(
                process_group.Launch(command, env, paths.worktree, streams, terminal),
                started=started,
            )
        if run.files.path("request.json").exists():
            run.files.path("request.json").read_text()
        run.files.write("phase", "running")
        limit = run_state.time_limit(
            run.request.kind, run.request.timeout, run.settings.max_run_minutes
        )
        try:
            return run_state.exit_status(child.wait(timeout=limit))
        except subprocess.TimeoutExpired:
            return 124
    finally:
        stack.enter_context(process_group.ignore_signals())
        if child is not None:
            stopped = _stop_harness(run, prepared, child)
            if stopped:
                with suppress(OSError, subprocess.SubprocessError):
                    child.wait()
                    run.files.write("phase", "stopped")
            if stopped and headless and run.request.harness is Harness.CODEX:
                with suppress(FileNotFoundError):
                    runfiles.copy_final(
                        run.tmpdir / "out.final", run.files.path("out.final")
                    )
        if terminal is not None:
            try:
                process_group.foreground(terminal, os.getpgrp())
            except OSError:
                with suppress(OSError):
                    run.files.write("log", "terminal unavailable during cleanup\n")


def stop_agent(agent: subprocess.Popen[str]) -> bool:
    stopped = process_group.stop_started(agent.pid, 0, agent.poll) is not False
    if stopped:
        agent.wait()
    return stopped


def prepared_result(result: object) -> Prepared:
    if not isinstance(result, Prepared):
        message = "run preparation returned no workspace"
        raise Invalid(message)
    return result


def execute(
    run: Run, text: str, push_token: str | None = None, *, preflight_only: bool = False
) -> int:
    """Keep cleanup protected until the completion record is durable."""
    if not run.request.unsandboxed:
        os.environ["PATH"] = host_tools.search()
    with host_tools.configured(run.settings, run.host.os), ExitStack() as stack:
        if not preflight_only:
            stack.enter_context(
                admission.reserve(admission.ROOT, run.host, run.request.profile)
            )
        return _execute(run, text, stack, push_token, preflight_only=preflight_only)


def _execute(
    run: Run,
    text: str,
    stack: ExitStack,
    push_token: str | credentials.PushTokenFailure | None = None,
    *,
    preflight_only: bool,
) -> int:
    prepared = None
    rc = 1
    agent = None
    try:
        if not run.files.path("request.json").exists():
            run.files.receive(run.request)
        run.files.write("phase", "check")
        preflight(run)
        result = process_group.prepare(
            lambda: prepare_workspace(run),
            run.files,
            run.deadline,
            run.settings.kill_grace_seconds,
        )
        prepared = prepared_result(result)
        runfiles.append(run.files.root, index_entry(run, prepared, None))
        run.files.write("phase", "workspace")
        run.files.write(
            "prompt", render.brief(run.request.kind, text, profile=run.request.profile)
        )
        run.files.write("phase", "brief")
        if prepared.delivery.signing is not None:
            agent = credentials.signing_agent(
                prepared.delivery.signing, run.tmpdir, run.deadline
            )
        if push_token is None and not preflight_only:
            push_token = credentials.push_token(run.request, run.settings)
        rc = (
            run_harness(run, prepared, stack, preflight_only=True)
            if preflight_only
            else push.run(run, prepared, stack, run_harness, push_token)
        )
    except TimeoutError, subprocess.TimeoutExpired:
        rc = 124
    except RunInterruptedError, EOFError:
        rc = 143
    except Invalid as exc:
        run.files.write("log", f"{exc}\n")
    except OSError, ValueError, subprocess.SubprocessError:
        run.files.write(
            "log", "run failed; check the clone and credential references\n"
        )
    finally:
        stack.enter_context(process_group.ignore_signals())
        rc = finish(run, prepared, agent, rc)
    return rc


def recover_auth(run: Run, destination: Path) -> Path:
    parent = runfiles.private_dir(run.home / ".codex-recovery")
    instant = datetime.now(UTC)
    while True:
        stamp = instant.strftime("%Y%m%dT%H%M%S.%fZ")
        directory = parent / f"{run.files.name}-{stamp}"
        try:
            directory.mkdir(mode=0o700)
            break
        except FileExistsError:
            instant += timedelta(microseconds=1)
    recovered = directory / "auth.json"
    destination.replace(recovered)
    return recovered


def finish_auth(run: Run, prepared: Prepared | None) -> str | None:
    if run.request.harness is not Harness.CODEX:
        return None
    paths = planned_paths(run) if prepared is None else prepared.paths
    reason = None
    if prepared is not None and not run.files.uncertain:
        destination = Path(paths.codex_home) / "auth.json"
        reason = credentials.auth_write_back(
            run.home / ".codex" / "auth.json", destination, prepared.auth_before
        )
        if reason is not None:
            recovered = recover_auth(run, destination)
            reason = f"{reason.rstrip()}; recovery: {recovered}\n"
    with suppress(FileNotFoundError):
        shutil.rmtree(paths.codex_home)
    return reason


def audit_summary(run: Run, prepared: Prepared | None, rc: int) -> None:
    paths = planned_paths(run) if prepared is None else prepared.paths
    audit = Path(paths.audit_dir)
    files: dict[str, str] = {}
    for path in sorted(audit.rglob("*")):
        if not path.is_symlink() and path.is_file():
            with path.open("rb") as stream:
                files[path.relative_to(audit).as_posix()] = hashlib.file_digest(
                    stream, hashlib.sha256
                ).hexdigest()
    would_refuse = _audit_findings(run)
    run.files.write(
        "audit.json",
        json.dumps({"rc": rc, "files": files, "would_refuse": would_refuse}) + "\n",
    )


def _bazel_roots(home: Path) -> tuple[tuple[Path, Path], ...]:
    """The (trusted anchor, cache root) pairs of the agent's own Bazel caches."""
    user = f"_bazel_{getpass.getuser()}"
    return (
        (home, home / ".cache/bazel" / user),
        (Path("/var/tmp"), Path("/var/tmp") / user),  # noqa: S108  # nosec B108 -- the trusted /var/tmp anchor, not a temp file
    )


def bazel_bases(home: Path, worktree: str) -> tuple[Path, ...]:
    """Pin the output bases Bazel keys by the md5 of the workspace path.

    Prepare time computes them once, so a worktree swapped later cannot name
    another workspace's base. Only these default bases qualify for deletion.
    """
    names = {
        hashlib.md5(path.encode(), usedforsecurity=False).hexdigest()
        for path in (worktree, os.path.realpath(worktree))
    }
    return tuple(
        root / name for _, root in _bazel_roots(home) for name in sorted(names)
    )


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _owned_dir(status: os.stat_result) -> bool:
    return stat.S_ISDIR(status.st_mode) and status.st_uid == os.getuid()


def _open_owned(home: Path, base: Path) -> tuple[int, os.stat_result] | None:
    """Open the base's parent and return its fd with the base's status.

    Every directory below the trusted anchor is opened with O_NOFOLLOW relative
    to the one before, and must be owned by the agent's uid, so a symlinked
    ancestor cannot redirect a deletion and the returned fd pins the validated
    chain even if a path is replaced later. The caller closes the fd.
    """
    anchor = next((a for a, root in _bazel_roots(home) if base.parent == root), None)
    if anchor is None or ".." in base.parts:
        return None
    names = base.relative_to(anchor).parts
    try:
        fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for name in names[:-1]:
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            if not _owned_dir(os.fstat(fd)):
                raise NotADirectoryError(name)
        status = os.stat(names[-1], dir_fd=fd, follow_symlinks=False)
        if not _owned_dir(status):
            raise NotADirectoryError(names[-1])
    except OSError:
        os.close(fd)
        return None
    return fd, status


def _owned_base(home: Path, base: Path) -> os.stat_result | None:
    """Return the base's lstat when it is a real directory the agent owns."""
    opened = _open_owned(home, base)
    if opened is None:
        return None
    os.close(opened[0])
    return opened[1]


def _server_alive(base: Path) -> bool:
    try:
        pid = int((base / "server/server.pid.txt").read_text())
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except PermissionError:
        # A live process of another uid, or an unreadable pid file: keep the base.
        return True
    except OSError, ValueError, OverflowError:
        # No readable pid file, or no such process, means no server we can see.
        return False
    return True


def _bazel_tool(run: Run) -> str | None:
    # The harness ran with the configured tool path, so search it as well.
    search = ":".join(
        (
            os.environ.get("PATH", os.defpath),
            policy.tool_path(run.settings, run.host, str(run.home)),
        )
    )
    return shutil.which("bazelisk", path=search) or shutil.which("bazel", path=search)


def _shutdown_once(tool: str, base: Path, worktree: str) -> str:
    try:
        done = subprocess.run(  # nosec B603
            [tool, f"--output_base={base}", "shutdown"],
            cwd=worktree,
            capture_output=True,
            timeout=_BAZEL_SHUTDOWN_SECONDS,
            check=False,
        )
        result = f"rc={done.returncode}"
    except subprocess.TimeoutExpired:
        result = "timed out"
    except OSError:
        result = "failed to run"
    if result == "rc=0" and _server_alive(base):
        result = "server alive"
    return result


def _shutdown_base(tool: str, base: Path, worktree: str) -> str:
    # A server still starting can answer shutdown with rc 0 before it accepts
    # commands, so a live server after a clean shutdown gets another attempt.
    result = _shutdown_once(tool, base, worktree)
    for _ in range(_BAZEL_SHUTDOWN_RETRIES):
        if result != "server alive":
            break
        time.sleep(_BAZEL_RETRY_SECONDS)
        result = _shutdown_once(tool, base, worktree)
    return result


def _recorded_bases(
    run: Run, prepared: Prepared
) -> dict[Path, tuple[Path, tuple[int, int]]]:
    recorded: dict[Path, tuple[Path, tuple[int, int]]] = {}
    for base in prepared.bases:
        status = _owned_base(run.home, base)
        if status is None:
            if os.path.lexists(base):
                run.files.write("log", f"bazel cleanup: refused {base}\n")
            continue
        recorded[Path(os.path.realpath(base))] = (base, (status.st_dev, status.st_ino))
    return recorded


def _shutdown_target(
    tool: str | None, base: Path, worktree: Path, server: bazel_servers.Server | None
) -> str:
    if tool is None:
        return "tool unavailable"
    if server is not None:
        if not bazel_servers.matches_pid(base, server.pid):
            return "server changed"
        if server.workspace.is_dir():
            worktree = server.workspace
    return _shutdown_base(tool, base, str(worktree))


def shutdown_bazel(
    run: Run,
    prepared: Prepared | None,
    *,
    group: int | None = None,
    servers: dict[Path, bazel_servers.Server] | None = None,
) -> BazelShutdown:
    """Stop the run's Bazel servers and mark recorded bases for deletion."""
    if prepared is None:
        return {}
    tool = _bazel_tool(run)
    worktree = Path(prepared.paths.worktree)
    recorded = _recorded_bases(run, prepared)
    targets: dict[Path, bazel_servers.Server | None] = dict.fromkeys(recorded)
    if servers is None and group is not None:
        servers = bazel_servers.find(worktree, group)
    targets.update(servers or {})
    stopped: BazelShutdown = {}
    for base, server in targets.items():
        if server is None and tool is None:
            continue
        if server is not None and not server.belongs:
            continue
        path, identity = recorded.get(base, (base, None))
        result = _shutdown_target(tool, path, worktree, server)
        run.files.write("log", f"bazel shutdown: {str(path)!r} ({result})\n")
        stopped[path] = (result, identity)
    return stopped


def _stop_harness(run: Run, prepared: Prepared, child: subprocess.Popen[str]) -> bool:
    with suppress(OSError):
        run.files.write("phase", "exited")
    # Snapshot ancestry before the harness group stops, but shut the servers
    # down after it: a shutdown issued while the harness is mid-build waits
    # for that build. Finish retries the recorded bases if an unexpected error
    # prevents the second pass from completing.
    servers: dict[Path, bazel_servers.Server] = {}
    with suppress(Exception):
        servers = bazel_servers.find(Path(prepared.paths.worktree), child.pid)
    stopped = process_group.stop_owned(
        run.files, child.pid, run.settings.kill_grace_seconds, child.poll
    )
    with suppress(Exception):
        run.bazel_shutdown = shutdown_bazel(run, prepared, servers=servers)
    return stopped


def _remove_base(parent_fd: int, name: str) -> None:
    """Delete a base relative to its parent's fd, unlocking only inside it.

    shutil.rmtree opens each directory through dir_fd where the platform allows,
    so it does not follow a symlink swapped in during the walk. The unlock chmod
    goes through the same fd and only touches directories the agent owns.
    """

    def unlock(_function: object, path: str, _error: BaseException) -> None:
        for target in {path, str(Path(path).parent)} - {"."}:
            with suppress(OSError):
                if _owned_dir(os.stat(target, dir_fd=parent_fd, follow_symlinks=False)):
                    os.chmod(target, 0o700, dir_fd=parent_fd)

    for _attempt in range(3):
        shutil.rmtree(name, onexc=unlock, dir_fd=parent_fd)
        if not _exists(parent_fd, name):
            return


def _exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _space(fd: int) -> tuple[int, int]:
    status = os.fstatvfs(fd)
    return status.f_bavail * status.f_frsize, status.f_favail


def _delete_base(
    run: Run, parent_fd: int, name: str, identity: tuple[int, int]
) -> tuple[int, int] | None:
    """Delete a base, returning the (bytes, inodes) freed, or None on failure.

    Re-checks the base's identity through the pinned parent fd just before removal.
    """
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise FileNotFoundError(name)
        before = _space(parent_fd)
        _remove_base(parent_fd, name)
        after = _space(parent_fd)
    except OSError:
        run.files.write("log", "bazel cleanup failed\n")
        return None
    if _exists(parent_fd, name):
        run.files.write("log", "bazel cleanup failed\n")
        return None
    return max(0, after[0] - before[0]), max(0, after[1] - before[1])


def _unlink_bazel_links(worktree: Path, deleted: list[Path]) -> None:
    for link in (*worktree.glob("bazel-*"), *worktree.glob(".bazel-*")):
        if not link.is_symlink():
            continue
        target = Path(os.path.normpath(link.parent / link.readlink()))
        if any(target.is_relative_to(base) for base in deleted):
            with suppress(OSError):
                link.unlink()


def _clean_base(
    run: Run, base: Path, result: str, identity: tuple[int, int]
) -> tuple[int, int] | None:
    """Delete one base unless it changed, its shutdown failed, or a server holds it."""
    opened = _open_owned(run.home, base)
    if opened is None:
        run.files.write("log", f"bazel cleanup: kept {base} ({result})\n")
        return None
    fd, status = opened
    try:
        if result != "rc=0" or (status.st_dev, status.st_ino) != identity:
            reason = result
        elif _server_alive(base):
            reason = "server alive"
        else:
            return _delete_base(run, fd, base.name, identity)
        run.files.write("log", f"bazel cleanup: kept {base} ({reason})\n")
        return None
    finally:
        os.close(fd)


def cleanup_bazel(
    run: Run,
    prepared: Prepared | None,
    stopped: BazelShutdown,
) -> dict[str, int]:
    """Delete the recorded output bases whose server is gone, and the links into them.

    A base is deleted only when its shutdown returned 0 and its server pid is
    dead at that moment, it is still the same directory (st_dev, st_ino) as at
    shutdown, and it is owned by the agent with no symlinked ancestor. The
    deletion runs relative to an fd that pins the validated ancestors. The freed
    counts are the statvfs delta on the base's filesystem across the deletion, so
    they include any other writer on that host, and a failed deletion adds nothing.
    """
    freed = {"bytes": 0, "inodes": 0, "bases": 0}
    if prepared is None:
        return freed
    deleted: list[Path] = []
    for base, (result, identity) in stopped.items():
        if identity is None:
            continue
        space = _clean_base(run, base, result, identity)
        if space is None:
            continue
        freed["bytes"] += space[0]
        freed["inodes"] += space[1]
        freed["bases"] += 1
        deleted.append(base)
    worktree = Path(prepared.paths.worktree)
    if deleted and not worktree.is_symlink():
        _unlink_bazel_links(worktree, deleted)
    if freed["bases"]:
        run.files.write("log", f"bazel cleanup: freed {freed}\n")
    return freed


def finish(
    run: Run, prepared: Prepared | None, agent: subprocess.Popen[str] | None, rc: int
) -> int:
    bazel = ""
    cleanup: dict[str, int] = {}
    try:
        try:
            reason = finish_auth(run, prepared)
            if reason is not None:
                rc = 1
                run.files.write("log", reason)
        except OSError, ValueError:
            rc = 1
            run.files.write("log", "codex auth write-back failed\n")
    finally:
        try:
            if agent is not None and stop_agent(agent) is False:
                process_group.unknown(run.files)
        except OSError, Invalid:
            process_group.unknown(run.files)
        finally:
            try:
                stopped = run.bazel_shutdown
                if stopped is None:
                    stopped = shutdown_bazel(run, prepared)
                bazel = ",".join(sorted({result for result, _ in stopped.values()}))
                cleanup = cleanup_bazel(run, prepared, stopped)
            except Exception:  # noqa: BLE001
                # Nothing from the Bazel cleanup may block the rc and completion record.
                with suppress(Exception):
                    run.files.write("log", "bazel cleanup failed\n")
            bazel = bazel or "skipped"
            try:
                phase = "unknown" if run.files.uncertain else "ended"
                run.files.write("phase", phase)
            except OSError:
                process_group.unknown(run.files)
            finally:
                run.files.write("rc", f"{rc}\n")
    try:
        audit_summary(run, prepared, rc)
    except OSError, ValueError:
        run.files.write("log", "audit summary write failed\n")
    try:
        entry = index_entry(run, prepared, rc) | {
            "bazel_shutdown": bazel,
            "bazel_cleanup": cleanup,
        }
        runfiles.append(run.files.root, entry)
    except OSError, ValueError:
        run.files.write("log", "completion index append failed\n")
    return rc


class RunInterruptedError(Exception):
    """Escape I/O without Python retrying a built-in InterruptedError."""


def interrupt(_number: int, _frame: object) -> None:
    if process_group.cleaning():
        return
    for number in process_group.INTERRUPTS:
        signal.signal(number, signal.SIG_IGN)
    raise RunInterruptedError


def refuse_started(files: runfiles.RunFiles) -> None:
    if files.path("rc").exists() or files.path("pgid").exists():
        message = f"run {files.name!r} has already started"
        raise RunExistsError(message)


def main(
    arguments: list[str],
    prog: str,
    *,
    push_token: str | None = None,
    claimed: bool = False,
) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("name", nargs="?")
    parser.add_argument("request", nargs="?")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--prompt")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--owned", action="store_true")
    args = parser.parse_args(arguments)
    if args.name is None:
        print(f"{prog}: run name is required", file=sys.stderr)
        return 1
    completed = None
    recorded = None
    previous = {s: signal.signal(s, interrupt) for s in process_group.INTERRUPTS}
    try:
        with process_group.defer_signals():
            files = _files(args.name)
            refuse_started(files)
            ownership.claim(files, claimed=claimed, owned=args.owned)
        # Anything that fails before this lock is held, including taking the
        # lock, belongs to a waiter and writes nothing: the records are the
        # run that holds the lock.
        with runfiles.locked(files.path("run.lock")):
            # Outside the try below: a refusal here, or an interrupt that
            # replaces it, must leave the earlier run's records alone.
            refuse_started(files)
            try:
                # Inside the try so a failure to create the temp directory
                # leaves a record like any other refusal.
                with tempfile.TemporaryDirectory() as tmpdir:
                    run, text = _load(args, Path(tmpdir).resolve())
                    run.files.write("log", build_info.line() + "\n")
                    completed = execute(
                        run, text, push_token, preflight_only=args.preflight
                    )
            except _REFUSALS as exc:
                if completed is None and not isinstance(exc, RunExistsError):
                    # Ignored until `recorded` is set: a signal in between
                    # would return a different status than the rc file holds.
                    with process_group.ignore_signals():
                        recorded = _recorded_rc(files)
                        if recorded is None:
                            _record_failure(files, exc)
                            recorded = _failure_rc(exc)
                raise
    except _REFUSALS as exc:
        if completed is not None:
            return completed
        print(f"{prog}: {exc}", file=sys.stderr)
        # An interrupt while the lock is released replaces the exception but
        # not the result already written to the rc file.
        return recorded if recorded is not None else _failure_rc(exc)
    else:
        return completed
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


_REFUSALS = (
    Invalid,
    userconfig.ConfigError,
    OSError,
    ValueError,
    RunInterruptedError,
)


def _failure_rc(exc: Exception) -> int:
    return 143 if isinstance(exc, RunInterruptedError) else 1


def _record_failure(files: runfiles.RunFiles, exc: Exception) -> None:
    """Write the failed run's records while the caller holds the run lock."""
    with process_group.ignore_signals():
        try:
            reason = (
                "interrupted during load"
                if isinstance(exc, RunInterruptedError)
                else str(exc)
            )
            files.write("log", f"run refused: {reason}\n")
        finally:
            files.write("rc", f"{_failure_rc(exc)}\n")


def _recorded_rc(files: runfiles.RunFiles) -> int | None:
    """The status `execute` already wrote, when a signal hit before main saw it."""
    path = files.path("rc")
    return int(path.read_text()) if path.exists() else None


def _files(name: str) -> runfiles.RunFiles:
    home = Path.home()
    root = runfiles.state_home(home) / "tbhbdev" / "agent-runs"
    return runfiles.RunFiles(root, name)


def _load(args: argparse.Namespace, tmpdir: Path) -> tuple[Run, str]:
    tmpdir = runfiles.private_dir(tmpdir / "tools")
    started = time.time()
    clock = time.monotonic()
    loaded = settings.load()
    host = next((h for h in loaded.hosts if h.name == args.host), None)
    if host is None:
        message = f"host {args.host!r} is not configured"
        raise Invalid(message)
    home = Path.home()
    files = _files(args.name)
    if files.path("request.json").exists() and args.request is not None:
        message = f"run {args.name!r} already exists"
        raise RunExistsError(message)
    raw = (
        sys.stdin.read()
        if args.request == "-"
        else Path(args.request or files.path("request.json")).read_text()
    )
    data = json.loads(raw)
    if not isinstance(data, dict):
        message = "request must be a JSON object"
        raise Invalid(message)
    parsed = request.parse(data, host_allows_unsandboxed=host.unsandboxed)
    if parsed.account not in host.accounts:
        message = f"account {parsed.account!r} is not enabled on {host.name}"
        raise Invalid(message)
    if parsed.harness is Harness.CLAUDE:
        profile = config.find_profile(
            loaded.claude.profiles, str(parsed.claude_profile)
        )
        if profile is None:
            message = f"claude profile {parsed.claude_profile!r} is not configured"
            raise Invalid(message)
        parsed = dataclasses.replace(parsed, claude_profile=profile.name)
    text = Path(args.prompt or files.path("prompt")).read_text()
    if args.request is not None:
        files.receive(parsed)
    run = Run(
        files,
        parsed,
        loaded,
        host,
        home,
        tmpdir,
        started,
        clock + parsed.startup_deadline_seconds,
    )
    return run, text
