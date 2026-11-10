"""The hidden host command: prepare, supervise and record one harness run."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import socket
import subprocess  # nosec B404
import sys
import tempfile
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

from tbhbdev import userconfig
from tbhbdev.alpha.agent import (
    credentials,
    host_tools,
    process_group,
    render,
    runfiles,
    settings,
    worktree,
)
from tbhbdev.alpha.agent.decisions import argv, config, policy, repo, request, run_state
from tbhbdev.alpha.agent.model import (
    AgyPolicy,
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


@dataclass(frozen=True)
class Run:
    files: runfiles.RunFiles
    request: RunRequest
    settings: Settings
    host: Host
    home: Path
    tmpdir: Path
    started: float
    deadline: float


@dataclass(frozen=True)
class Prepared:
    paths: RunPaths
    delivery: credentials.Delivery
    auth_before: str | None = dataclasses.field(repr=False)


def planned_paths(run: Run) -> RunPaths:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    workspace = clone / ".worktrees" / f"agent-{run.files.name}"
    return RunPaths(
        str(run.home),
        str(workspace.resolve()),
        str(run.tmpdir),
        str(run.tmpdir / "codex"),
        str((run.home / run.settings.sandbox.audit_dir).resolve()),
        str(workspace / ".git"),
        uid=os.getuid(),
    )


# These files can alter the next trusted shell or tool invocation.
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
    ".ssh",
    ".claude",
    ".claude.json",
    ".codex",
)


def protected_home(run: Run) -> tuple[str, ...]:
    sockets = run.home / run.settings.sockets_dir
    # HOME itself is the boundary: protecting it would refuse every workspace.
    parents = tuple(
        p for p in sockets.parents if p.is_relative_to(run.home) and p != run.home
    )
    clone = Path(repo.clone_path(run.home, run.request.repo))
    account_git = clone / ".git"
    if account_git.is_file():
        message = "account clone must have its own git directory"
        raise Invalid(message)
    mirror = worktree.mirror_path(run.home, run.request.repo)
    paths = (
        *(run.home / p for p in _STARTUP_FILES),
        *parents,
        account_git,
        mirror,
        mirror.parent,
        mirror.parent.parent,
        userconfig.path(),
    )
    return tuple(dict.fromkeys(str(alias) for p in paths for alias in (p, p.resolve())))


def full_denies(run: Run, paths: RunPaths) -> tuple[str, ...]:
    candidates = policy.denied_paths(run.settings, run.host, paths.home, paths.uid)
    mirror = str(worktree.mirror_path(run.home, run.request.repo))
    return tuple(
        dict.fromkeys(
            alias for p in (*candidates, mirror) for alias in (p, os.path.realpath(p))
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


def checked_policy(run: Run, paths: RunPaths) -> tuple[Policy, str]:
    """Check directories before even inspecting or invoking an executable."""
    directories = host_tools.search_path(run.settings, run.host.os)
    entries = run.settings.sandbox.path
    sandbox_directories = tuple(
        str(run.home / p)
        for p in (entries.linux if run.host.os is Os.LINUX else entries.macos)
    )
    protected = (
        str(run.files.root.resolve()),
        *protected_home(run),
        *host_tools.installation(),
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
    sandbox = run.settings.sandbox
    check_settings = dataclasses.replace(
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
    policy.protect_host(
        check_settings, resolved, (*protected, *full_denies(run, resolved))
    )
    clone = Path(repo.clone_path(run.home, run.request.repo))
    policy.protect_paths(
        (
            *(str(run.home / p) for p in check_settings.sandbox.write_roots),
            *(str(run.home / p) for p in check_settings.sandbox.agy.build_write),
        ),
        (str(clone), str(clone.resolve())),
    )
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
    name = (
        "agy-agent"
        if run.request.harness is Harness.AGY and run.request.profile is Profile.BUILD
        else run.request.harness.value
    )
    executable = host_tools.resolve(name, value.env["PATH"])
    commands = [
        host_tools.resolve(name, ":".join(directories))
        for name in ("git", "ssh-agent", "ssh-add", "ssh", "openssl")
    ]
    if run.host.os.value == "macos":
        commands.extend(
            host_tools.resolve(name, ":".join(directories))
            for name in ("sysctl", "ps", "security")
        )
    executables = (
        *host_tools.executable_paths(executable, value.env["PATH"]),
        *(
            p
            for command in commands
            for p in host_tools.executable_paths(command, ":".join(directories))
        ),
    )
    policy.protect_host(check_settings, resolved, executables)
    return value, executable


def preflight(run: Run) -> None:
    _value, executable = checked_policy(run, planned_paths(run))
    sandbox = run.settings.sandbox
    reason = (
        sandbox.agy_unsandboxed_opt_out
        if run.request.harness is Harness.AGY
        else sandbox.codex_unsandboxed_opt_out
        if run.request.harness is Harness.CODEX and run.request.unsandboxed
        else sandbox.codex_network_proxy_opt_out
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
        and not host_tools.codex_proxy_supported(executable, run.tmpdir)
    ):
        message = (
            "Codex network_proxy support required (minimum 0.164.0); "
            "upgrade or configure codex_network_proxy_opt_out"
        )
        raise Invalid(message)


def prepare_workspace(run: Run) -> Prepared:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    paths = planned_paths(run)
    checked_policy(run, paths)
    workspace = worktree.create(clone, run.files.name, run.request)
    delivery = credentials.deliver(run.request, run.settings, run.tmpdir)
    paths = RunPaths(
        home=str(run.home),
        worktree=str(workspace),
        tmpdir=str(run.tmpdir),
        codex_home=str(run.tmpdir / "codex"),
        audit_dir=policy.resolve(str(run.home), run.settings.sandbox.audit_dir),
        git_common_dir=str(workspace / ".git"),
        uid=os.getuid(),
        signing_socket=str(run.tmpdir / "ssh.sock")
        if delivery.signing is not None
        else None,
    )
    runfiles.private_dir(Path(paths.audit_dir))
    before = None
    if run.request.harness is Harness.CODEX:
        runfiles.private_dir(Path(paths.codex_home))
        source = runfiles.private_dir(run.home / ".codex") / "auth.json"
        before = credentials.auth_copy(source, Path(paths.codex_home) / "auth.json")
    return Prepared(paths, delivery, before)


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


def run_harness(run: Run, prepared: Prepared, stack: ExitStack) -> int:
    paths = prepared.paths
    value, executable = checked_policy(run, paths)
    env = value.env | prepared.delivery.env
    command = harness_command(run, prepared, value, executable)
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
            with suppress(OSError):
                run.files.write("phase", "exited")
            stopped = process_group.stop_owned(
                run.files, child.pid, run.settings.kill_grace_seconds, child.poll
            )
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


def execute(run: Run, text: str) -> int:
    """Keep cleanup protected until the completion record is durable."""
    with host_tools.configured(run.settings, run.host.os), ExitStack() as stack:
        return _execute(run, text, stack)


def _execute(run: Run, text: str, stack: ExitStack) -> int:
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
        run.files.write("prompt", render.brief(run.request.kind, text))
        run.files.write("phase", "brief")
        if prepared.delivery.signing is not None:
            agent = credentials.signing_agent(
                prepared.delivery.signing, run.tmpdir, run.deadline
            )
        rc = run_harness(run, prepared, stack)
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


def finish(
    run: Run, prepared: Prepared | None, agent: subprocess.Popen[str] | None, rc: int
) -> int:
    try:
        if (
            prepared is not None
            and run.request.harness is Harness.CODEX
            and not run.files.uncertain
        ):
            try:
                reason = credentials.auth_write_back(
                    run.home / ".codex" / "auth.json",
                    Path(prepared.paths.codex_home) / "auth.json",
                    prepared.auth_before,
                )
                if reason is not None:
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
                phase = "unknown" if run.files.uncertain else "ended"
                run.files.write("phase", phase)
            except OSError:
                process_group.unknown(run.files)
            finally:
                run.files.write("rc", f"{rc}\n")
    try:
        runfiles.append(run.files.root, index_entry(run, prepared, rc))
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


class RunExistsError(Invalid):
    """A refused rerun must preserve its previous owner's records."""


def refuse_started(files: runfiles.RunFiles) -> None:
    if files.path("rc").exists() or files.path("pgid").exists():
        message = f"run {files.name!r} has already started"
        raise RunExistsError(message)


def main(arguments: list[str], prog: str) -> int:
    os.environ["PATH"] = host_tools.search()
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("name", nargs="?")
    parser.add_argument("request", nargs="?")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--prompt")
    args = parser.parse_args(arguments)
    if args.name is None:
        print(f"{prog}: run name is required", file=sys.stderr)
        return 1
    files = None
    completed = None
    previous = {s: signal.signal(s, interrupt) for s in process_group.INTERRUPTS}
    try:
        with process_group.defer_signals():
            files = _files(args.name)
            refuse_started(files)
        with (
            runfiles.locked(files.path("run.lock")),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            refuse_started(files)
            run, text = _load(args, Path(tmpdir).resolve())
            completed = execute(run, text)
    except (
        Invalid,
        userconfig.ConfigError,
        OSError,
        ValueError,
        RunInterruptedError,
    ) as exc:
        if completed is not None:
            return completed
        rc = 143 if isinstance(exc, RunInterruptedError) else 1
        with process_group.ignore_signals():
            if files is not None and not isinstance(exc, RunExistsError):
                try:
                    reason = (
                        "interrupted during load"
                        if isinstance(exc, RunInterruptedError)
                        else str(exc)
                    )
                    files.write("log", f"run refused: {reason}\n")
                finally:
                    files.write("rc", f"{rc}\n")
        print(f"{prog}: {exc}", file=sys.stderr)
        return rc
    else:
        return completed
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _files(name: str) -> runfiles.RunFiles:
    home = Path.home()
    root = (
        Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
        / "tbhbdev"
        / "agent-runs"
    )
    return runfiles.RunFiles(root, name)


def _load(args: argparse.Namespace, tmpdir: Path) -> tuple[Run, str]:
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
