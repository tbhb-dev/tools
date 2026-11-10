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
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from tbhbdev import userconfig
from tbhbdev.alpha.agent import (
    credentials,
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


def prepare_workspace(run: Run) -> Prepared:
    clone = Path(repo.clone_path(run.home, run.request.repo))
    workspace = worktree.create(clone, run.files.name, run.request)
    delivery = credentials.deliver(run.request, run.settings, run.tmpdir)
    paths = RunPaths(
        home=str(run.home),
        worktree=str(workspace),
        tmpdir=str(run.tmpdir),
        codex_home=str(run.tmpdir / "codex"),
        audit_dir=policy.resolve(str(run.home), run.settings.sandbox.audit_dir),
        git_common_dir=str(worktree.common_dir(clone)),
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


def run_harness(run: Run, prepared: Prepared, stack: ExitStack) -> int:
    paths = prepared.paths
    value = policy.build(
        run.settings,
        run.host,
        harness=run.request.harness,
        profile=run.request.profile,
        account=run.request.account,
        unsandboxed=run.request.unsandboxed,
        paths=paths,
    )
    env = value.env | prepared.delivery.env
    text = run.files.path("prompt").read_text()
    project = (
        _agy_project(run, prepared, value.detail)
        if isinstance(value.detail, AgyPolicy)
        else None
    )
    command = argv.harness(
        run.request,
        value,
        worktree=paths.worktree,
        brief_file=str(run.files.path("prompt")),
        agy_project=project,
        agy_brief=text,
    )
    headless = run.request.kind is RunKind.EXEC
    if headless and run.request.harness is Harness.CODEX:
        command = (
            *command[:-1],
            "--output-last-message",
            str(run.files.path("out.final")),
            command[-1],
        )
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
            try:
                run.files.write("phase", "exited")
            finally:
                process_group.stop_started(child.pid, run.settings.kill_grace_seconds)
                child.wait()
            run.files.write("phase", "stopped")
        if terminal is not None:
            try:
                process_group.foreground(terminal, os.getpgrp())
            except OSError:
                run.files.write("log", "terminal unavailable during cleanup\n")


def stop_agent(agent: subprocess.Popen[str]) -> None:
    process_group.stop_started(agent.pid, 0)
    agent.wait()


def prepared_result(result: object) -> Prepared:
    if not isinstance(result, Prepared):
        message = "run preparation returned no workspace"
        raise Invalid(message)
    return result


def execute(run: Run, text: str) -> int:
    """Keep cleanup protected until the completion record is durable."""
    with ExitStack() as stack:
        return _execute(run, text, stack)


def _execute(run: Run, text: str, stack: ExitStack) -> int:
    prepared = None
    rc = 1
    agent = None
    try:
        if not run.files.path("request.json").exists():
            run.files.receive(run.request)
        run.files.write("phase", "check")
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
        if prepared is not None and run.request.harness is Harness.CODEX:
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
            if agent is not None:
                stop_agent(agent)
        finally:
            try:
                run.files.write("phase", "ended")
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


def main(arguments: list[str], prog: str) -> int:
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
    previous = {s: signal.signal(s, interrupt) for s in process_group.INTERRUPTS}
    try:
        with process_group.defer_signals():
            files = _files(args.name)
        with (
            runfiles.locked(files.path("run.lock")),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            run, text = _load(args, Path(tmpdir).resolve())
            return execute(run, text)
    except (
        Invalid,
        userconfig.ConfigError,
        OSError,
        ValueError,
        RunInterruptedError,
    ) as exc:
        rc = 143 if isinstance(exc, RunInterruptedError) else 1
        with process_group.ignore_signals():
            if files is not None:
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
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _files(name: str) -> runfiles.RunFiles:
    home = Path.home()
    root = (
        Path(os.environ.get("XDG_STATE_HOME", str(home / ".local" / "state")))
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
        raise Invalid(message)
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
    if files.path("rc").exists() or files.path("pgid").exists():
        message = f"run {args.name!r} has already started"
        raise Invalid(message)
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
