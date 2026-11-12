"""The hidden host command: prepare, supervise and record one harness run."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
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
        str(runfiles.state_home(run.home) / "tbhbdev" / "agent-codex" / run.files.name),
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
    ".config/mise",
    ".ssh",
    ".claude",
    ".claude.json",
    ".codex",
    ".codex-recovery",
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
    write_roots = (
        resolved.codex_home,
        resolved.worktree,
        resolved.tmpdir,
        resolved.audit_dir,
        "/dev/fd",
        *check_settings.sandbox.write_roots,
        *check_settings.sandbox.agy.build_write,
    )
    worktree.validate_writes(Path(resolved.worktree), clone, write_roots)
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
    if run.request.harness is Harness.AGY:
        message = "agy runs are refused: no OS sandbox"
        raise Invalid(message)
    if run.request.harness not in run.host.proven_harnesses:
        message = (
            f"{run.request.harness} has no recorded sandbox pass on {run.host.name}"
        )
        raise Invalid(message)
    _value, executable = checked_policy(run, planned_paths(run))
    if run.request.profile is Profile.REVIEW and run.request.tainted_host:
        message = "review runs are refused on a tainted host"
        raise Invalid(message)
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
        and not host_tools.codex_proxy_supported(executable, run.tmpdir)
    ):
        message = (
            "Codex network_proxy support required (minimum 0.164.0); "
            "upgrade or configure codex_network_proxy_opt_out"
        )
        raise Invalid(message)

    if run.request.harness is Harness.CLAUDE:
        message = (
            "claude runs are refused: separate tool paths for Claude (H3) "
            "and the Claude credential decision remain open"
        )
        raise Invalid(message)
    if run.request.unsandboxed:
        mark_host(run)


def prepare_workspace(run: Run) -> Prepared:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    paths = planned_paths(run)
    checked_policy(run, paths)
    workspace = worktree.create(
        clone, run.files.name, run.request, run.settings.public_repos
    )
    with worktree.preparation_step("credential delivery"):
        delivery = credentials.deliver(run.request, run.settings, run.tmpdir)
    paths = RunPaths(
        home=str(run.home),
        worktree=str(workspace),
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


def log_hook_tools(run: Run, paths: RunPaths) -> None:
    status = (
        "present"
        if (Path(paths.worktree) / ".bazel_env" / "bin").is_dir()
        else "missing; commit hooks that call .bazel_env/bin will fail in this run"
    )
    run.files.write("log", f"preflight: .bazel_env {status}\n")


def run_harness(
    run: Run, prepared: Prepared, stack: ExitStack, *, preflight_only: bool = False
) -> int:
    paths = prepared.paths
    value, executable = checked_policy(run, paths)
    env = value.env | prepared.delivery.env
    command = harness_command(run, prepared, value, executable)
    if preflight_only:
        log_hook_tools(run, paths)
        run.files.write("log", f"preflight ok: {executable}\n")
        return 0
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


def execute(
    run: Run, text: str, push_token: str | None = None, *, preflight_only: bool = False
) -> int:
    """Keep cleanup protected until the completion record is durable."""
    with host_tools.configured(run.settings, run.host.os), ExitStack() as stack:
        return _execute(run, text, stack, push_token, preflight_only=preflight_only)


def _execute(
    run: Run,
    text: str,
    stack: ExitStack,
    push_token: str | None = None,
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
        run.files.write("prompt", render.brief(run.request.kind, text))
        run.files.write("phase", "brief")
        if prepared.delivery.signing is not None:
            agent = credentials.signing_agent(
                prepared.delivery.signing, run.tmpdir, run.deadline
            )
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


def finish(
    run: Run, prepared: Prepared | None, agent: subprocess.Popen[str] | None, rc: int
) -> int:
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
    os.environ["PATH"] = host_tools.search()
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("name", nargs="?")
    parser.add_argument("request", nargs="?")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--prompt")
    parser.add_argument("--owner")
    parser.add_argument("--preflight", action="store_true")
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
            ownership.claim(files, claimed=claimed, owner=args.owner)
        with (
            runfiles.locked(files.path("run.lock")),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            refuse_started(files)
            run, text = _load(args, Path(tmpdir).resolve())
            completed = execute(run, text, push_token, preflight_only=args.preflight)
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
