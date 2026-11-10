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
    child = process_group.launch(
        command, env=env, cwd=paths.worktree, streams=streams, terminal=terminal
    )
    try:
        process_group.record(run.files, child.pid)
        run.files.write("launched", "\n")
        run.files.write("phase", "running")
        limit = run_state.time_limit(
            run.request.kind, run.request.timeout, run.settings.max_run_minutes
        )
        try:
            return run_state.exit_status(child.wait(timeout=limit))
        except subprocess.TimeoutExpired:
            return 124
    finally:
        run.files.write("phase", "exited")
        process_group.stop_started(child.pid, run.settings.kill_grace_seconds)
        child.wait()
        run.files.write("phase", "stopped")
        if terminal is not None:
            process_group.foreground(terminal, os.getpgrp())


def stop_agent(agent: subprocess.Popen[str]) -> None:
    process_group.stop_started(agent.pid, 0)
    agent.wait()


def prepared_result(result: object) -> Prepared:
    if not isinstance(result, Prepared):
        message = "run preparation returned no workspace"
        raise Invalid(message)
    return result


def execute(run: Run, text: str) -> int:
    """All exits write rc after group cleanup, auth refresh and index append."""
    prepared = None
    rc = 1
    agent = None
    try:
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
        with ExitStack() as stack:
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
        if prepared is not None:
            try:
                if run.request.harness is Harness.CODEX:
                    credentials.auth_write_back(
                        run.home / ".codex" / "auth.json",
                        Path(prepared.paths.codex_home) / "auth.json",
                        prepared.auth_before,
                    )
            except OSError, ValueError:
                rc = 1
                run.files.write("log", "codex auth write-back failed\n")
            if agent is not None:
                stop_agent(agent)
        runfiles.append(run.files.root, index_entry(run, prepared, rc))
        run.files.write("phase", "ended")
        run.files.write("rc", f"{rc}\n")
    return rc


class RunInterruptedError(Exception):
    """Escape I/O without Python retrying a built-in InterruptedError."""


def interrupt(_number: int, _frame: object) -> None:
    raise RunInterruptedError


def main(arguments: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("name")
    parser.add_argument("request", nargs="?")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--prompt")
    args = parser.parse_args(arguments)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            run, text = _load(args, Path(tmpdir))
            previous = {
                s: signal.signal(s, interrupt)
                for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
            }
            try:
                return execute(run, text)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
    except (Invalid, userconfig.ConfigError, OSError, ValueError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1


def _load(args: argparse.Namespace, tmpdir: Path) -> tuple[Run, str]:
    started = time.time()
    clock = time.monotonic()
    loaded = settings.load()
    host = next((h for h in loaded.hosts if h.name == args.host), None)
    if host is None:
        message = f"host {args.host!r} is not configured"
        raise Invalid(message)
    home = Path.home()
    root = (
        Path(os.environ.get("XDG_STATE_HOME", str(home / ".local" / "state")))
        / "tbhbdev"
        / "agent-runs"
    )
    files = runfiles.RunFiles(root, args.name)
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
