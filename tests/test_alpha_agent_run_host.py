"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from agent_host_example import constant, host_settings, repository, run_request
from test_alpha_agent_credentials import VALUES
from test_alpha_agent_settings import EXAMPLE

from tbhbdev import secrets
from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import (
    credentials,
    host_tools,
    process_group,
    render,
    run_host,
    runfiles,
)
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.decisions.run_state import Group
from tbhbdev.alpha.agent.model import CodexPolicy, Harness, Invalid, Os, RunKind

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

    from tbhbdev.alpha.agent.model import Policy, RunRequest


@pytest.fixture(autouse=True)
def fake_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def read(reference: str) -> str:
        return VALUES[reference]

    def installation(*_args: object) -> str:
        return "example-installation-credential"

    monkeypatch.setenv("HOME", str(runfiles.private_dir(tmp_path / "operator-home")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(secrets, "read", read)
    monkeypatch.setattr(credentials, "installation_token", installation)
    real_resolve = host_tools.resolve

    def resolve(name: str, path: str | None = None) -> str:
        return (
            "/usr/bin/true"
            if name in ("codex", "claude", "agy", "agy-agent", "prek")
            else real_resolve(name, path)
        )

    monkeypatch.setattr(host_tools, "resolve", resolve)
    monkeypatch.setattr(host_tools, "codex_proxy_supported", constant(value=True))


@pytest.fixture(autouse=True)
def portable_run_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check protocol spelling even on a case-insensitive filesystem."""
    original_path = runfiles.RunFiles.path
    original_copy = runfiles.copy_final

    def path(files: runfiles.RunFiles, suffix: str) -> Path:
        assert suffix == suffix.lower()
        return original_path(files, suffix)

    def copy(source: Path, destination: Path) -> None:
        assert source.name == "out.final"
        assert destination.name.endswith(".out.final")
        original_copy(source, destination)

    monkeypatch.setattr(runfiles.RunFiles, "path", path)
    monkeypatch.setattr(runfiles, "copy_final", copy)


def make_run(tmp_path: Path, **changes: object) -> run_host.Run:
    files = runfiles.RunFiles(tmp_path / "runs", "example")
    value = run_request(**changes)
    home = runfiles.private_dir(tmp_path / "home")
    tmpdir = runfiles.private_dir(tmp_path / "private-tmp")
    return run_host.Run(
        files,
        value,
        host_settings(),
        host_settings().hosts[0],
        home,
        tmpdir,
        1000,
        time.monotonic() + 10,
    )


def preparation(run: run_host.Run) -> run_host.Prepared:
    paths = run_host.RunPaths(
        str(run.home),
        str(runfiles.private_dir(run.home / "worktree")),
        str(run.tmpdir),
        str(runfiles.private_dir(run.tmpdir / "codex")),
        str(run.tmpdir / "audit"),
    )
    return run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )


@pytest.mark.parametrize("harness", Harness)
def test_prepare_uses_one_worktree_and_private_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        profile="review",
        commit="0" * 40,
        timeout=900,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example" if harness is Harness.CLAUDE else None,
    )
    _, clone = repository(run.home / "Code" / "github.com" / "example-org")
    clone.rename(clone.parent / "example-repo")
    clone = clone.parent / "example-repo"
    monkeypatch.setattr(
        run_host.worktree.repo, "ssh_url", constant(str(clone.parent / "origin"))
    )
    commit = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()
    run = dataclasses.replace(
        run, request=dataclasses.replace(run.request, commit=commit)
    )
    source = runfiles.private_dir(run.home / ".codex") / "auth.json"
    source.write_text("example-auth")
    result = run_host.prepare_workspace(run)
    assert result.paths.uid == os.getuid()
    assert result.paths.home == str(run.home)
    assert result.paths.worktree == str(clone / ".worktrees" / "agent-example")
    assert result.paths.tmpdir == str(run.tmpdir)
    assert result.paths.codex_home == str(run.tmpdir / "codex")
    assert result.paths.git_common_dir == str(Path(result.paths.worktree) / ".git")
    assert result.paths.signing_socket is None
    assert result.paths.audit_dir == str(run.home / run.settings.sandbox.audit_dir)
    assert Path(result.paths.audit_dir).is_dir()
    assert result.delivery.gh_fallback is False
    assert result.auth_before == ("example-auth" if harness is Harness.CODEX else None)
    assert (run.tmpdir / "gh" / "hosts.yml").is_file()
    copied_auth = Path(result.paths.codex_home) / "auth.json"
    assert copied_auth.exists() is (harness is Harness.CODEX)
    if harness is Harness.CODEX:
        assert copied_auth.read_text() == "example-auth"


def test_build_preparation_has_the_run_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    def create(_clone: Path, _name: str, _value: run_host.RunRequest) -> Path:
        return runfiles.private_dir(run.home / "worktree")

    monkeypatch.setattr(run_host.worktree, "create", create)
    result = run_host.prepare_workspace(run)
    assert result.paths.signing_socket == str(run.tmpdir / "ssh.sock")
    assert result.delivery.signing == "example-signing"


class Child:
    pid = 23

    def __init__(self, code: int = 7, *, timeout: bool = False) -> None:
        self.code = code
        self.timeout = timeout
        self.waits: list[int | None] = []

    def wait(self, timeout: int | None = None) -> int:
        self.waits.append(timeout)
        if self.timeout and len(self.waits) == 1:
            assert timeout is not None
            command = "example-harness"
            raise subprocess.TimeoutExpired(command, timeout)
        return self.code


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("kind", RunKind)
def test_launch_uses_the_policy_and_marks_only_after_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, kind: RunKind
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        kind=kind.value,
        timeout=900,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example" if harness is Harness.CLAUDE else None,
    )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, max_run_minutes=1)
    )
    prepared = preparation(run)
    env = (
        {credentials.CLAUDE_TOKEN_ENV: "example-claude"}
        if harness is Harness.CLAUDE
        else {}
    )
    prepared = dataclasses.replace(
        prepared,
        delivery=credentials.Delivery(
            env, None, gh_fallback=False, claude_profile="primary" if env else None
        ),
    )
    run.files.write("prompt", render.brief(kind, "example task"))
    metadata: list[tuple[str, str]] = []
    real_write = run.files.write

    def write(suffix: str, text: str) -> None:
        metadata.append((suffix, text))
        real_write(suffix, text)

    monkeypatch.setattr(run.files, "write", write)
    commands = check_harness_arguments(run, prepared, monkeypatch)
    child = Child()
    events: list[object] = []
    monkeypatch.setenv("EXAMPLE_PARENT_MARKER", "example-parent-credential")
    monkeypatch.setenv("EXAMPLE_TMUX_MARKER", "example-tmux-credential")

    def launch(spec: process_group.Launch, **kwargs: object) -> Child:
        command = spec.command
        kwargs.update(
            env=spec.env, cwd=spec.cwd, streams=spec.streams, terminal=spec.terminal
        )
        check_launch_contract(run, prepared, command, kwargs, commands[-1])
        events.append("spawn")
        cast("Callable[[Child], None]", kwargs["started"])(child)
        return child

    def record(files: runfiles.RunFiles, pid: int) -> None:
        assert files is run.files
        assert pid == 23
        assert not files.path("launched").exists()
        events.append("record")

    def stop(pid: int, grace: float) -> None:
        assert (pid, grace) == (23, 0)
        assert run.files.path("launched").exists()
        assert run.files.path("phase").read_text() == "exited"
        events.append("stop")

    def foreground(fd: int, group: int) -> None:
        assert fd == 0
        assert group == os.getpgrp()
        events.append("foreground")

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", record)
    monkeypatch.setattr(process_group, "stop_started", stop)
    monkeypatch.setattr(process_group, "foreground", foreground)
    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    with ExitStack() as stack:
        assert run_host.run_harness(run, prepared, stack) == 7
    assert run.files.path("phase").read_text() == "stopped"
    assert events == [
        "spawn",
        "record",
        "stop",
        *(["foreground"] if kind is RunKind.START else []),
    ]
    assert metadata == [
        ("launched", "\n"),
        ("phase", "running"),
        ("phase", "exited"),
        ("phase", "stopped"),
    ]
    assert child.waits == [60 if kind is RunKind.EXEC else 900, None]
    assert run.files.path("out").exists() is (kind is RunKind.EXEC)


@pytest.mark.parametrize("expired", [False, True])
def test_harness_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    run = make_run(tmp_path, timeout=1)
    run = dataclasses.replace(run, deadline=1000 if expired else 1001)
    monkeypatch.setattr(time, "monotonic", lambda: 1000)
    prepared = preparation(run)
    run.files.write("prompt", "example")
    child = Child(timeout=True)

    def launch(*_args: object, **kwargs: object) -> Child:
        cast("Callable[[Child], None]", kwargs["started"])(child)
        return child

    def record(_files: runfiles.RunFiles, _pid: int) -> None:
        return None

    stopped: list[int] = []

    def stop(pid: int, _grace: float) -> None:
        stopped.append(pid)

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", record)
    monkeypatch.setattr(process_group, "stop_started", stop)
    with ExitStack() as stack:
        if expired:
            with pytest.raises(TimeoutError):
                run_host.run_harness(run, prepared, stack)
            assert stopped == []
        else:
            assert run_host.run_harness(run, prepared, stack) == 124
            assert stopped == [23]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (None, 7),
        ("timeout", 124),
        ("interrupt", 143),
        ("invalid", 1),
        ("os", 1),
        ("auth", 1),
        ("result", 1),
    ],
)
def test_finish_orders_cleanup_auth_agent_index_and_atomic_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None, code: int
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    prepared = dataclasses.replace(
        prepared,
        auth_before="example-before",
        delivery=credentials.Delivery(
            {}, "example-key", gh_fallback=False, claude_profile=None
        ),
    )
    runtime = HostRuntime(run, prepared, failure)
    runtime.patch(monkeypatch)
    assert run_host.execute(run, "example") == code
    assert run.files.path("rc").read_text() == f"{code}\n"
    assert run.files.path("phase").read_text() == "ended"
    assert run.files.path("request.json").stat().st_mode & 0o777 == 0o600
    assert "example-key" not in run.files.path("request.json").read_text()
    if failure in (None, "auth"):
        assert runtime.events == [
            "agent-started",
            "group-stopped",
            "auth",
            "agent-stopped",
        ]
        line = runfiles.latest(run.files.root)["example"]
        assert line["worktree"] == prepared.paths.worktree
        assert line["state"] == "ended"
        assert line["rc"] == code
        assert line["trace_id"] is None
        assert line["v"] == 1
    else:
        assert not run.files.path("launched").exists()
    assert runtime.phases == (
        ["check", "workspace", "brief", "ended"]
        if failure in (None, "auth")
        else ["check", "ended"]
    )
    messages = {
        "invalid": "safe preparation error\n",
        "os": "run failed; check the clone and credential references\n",
        "auth": "codex auth write-back failed\n",
        "result": "run preparation returned no workspace\n",
    }
    if failure in messages:
        assert run.files.path("log").read_text() == messages[failure]
    else:
        assert not run.files.path("log").exists()
    if failure in (None, "auth"):
        assert run.files.path("prompt").read_text() == render.brief(
            run.request.kind, "example"
        )
        entries = [
            json.loads(line)
            for line in (run.files.root / runfiles.INDEX).read_text().splitlines()
        ]
        assert entries[0]["worktree"] == prepared.paths.worktree
        assert entries[0]["state"] == "running"
        assert entries[0]["rc"] is None


def test_interrupt_and_agent_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    with process_group.defer_signals(), pytest.raises(run_host.RunInterruptedError):
        run_host.interrupt(15, None)
    child = Child()
    stopped: list[int] = []

    def stop(pid: int, grace: float) -> None:
        assert grace == 0
        stopped.append(pid)

    monkeypatch.setattr(process_group, "stop_started", stop)
    run_host.stop_agent(cast("subprocess.Popen[str]", child))
    assert stopped == [23]
    assert child.waits == [None]
    with process_group.ignore_signals():
        assert run_host.interrupt(15, None) is None


@pytest.mark.parametrize("source", ["file", "stdin", "recorded"])
def test_hidden_dispatch_reads_request_and_restores_handlers(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    config_file.write_text(EXAMPLE)
    expected_settings = run_host.settings.load()
    files = runfiles.RunFiles(
        Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs", "example"
    )
    files.write("prompt", "example task")
    files.write("json", "driver metadata")
    value = run_request(harness="claude", claude_profile="Primary", reason="example")
    raw = json.dumps(request.to_dict(value))
    arguments = ["run-host", "example", "--host", "example-linux"]
    if source == "recorded":
        files.write("request.json", raw)
    elif source == "stdin":
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        arguments.insert(2, "-")
    else:
        path = tmp_path / "received.json"
        path.write_text(raw)
        arguments.insert(2, str(path))
        prompt = tmp_path / "prompt"
        prompt.write_text("example task")
        arguments += ["--prompt", str(prompt)]
    before = {
        s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    }

    def execute(run: run_host.Run, text: str) -> int:
        assert run.settings == expected_settings
        assert run.request.claude_profile == "primary"
        assert run.files.root == files.root
        assert files.path("run.lock").is_file()
        assert run.home == Path.home()
        assert text == "example task"
        assert run.tmpdir.is_dir()
        assert 0 < run.started <= time.time()
        assert 0 < run.deadline - time.monotonic() <= 120
        for number in before:
            assert signal.getsignal(number) is run_host.interrupt
        return 9

    monkeypatch.setattr(run_host, "execute", execute)
    assert agent.main(arguments, "example agent") == 9
    assert {s: signal.getsignal(s) for s in before} == before
    assert files.path("json").read_text() == "driver metadata"


@pytest.mark.parametrize(
    "case", ["host", "list", "duplicate", "rc", "pgid", "account", "profile", "missing"]
)
def test_command_refuses_invalid_input(
    tmp_path: Path,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    config_file.write_text(EXAMPLE)
    files = runfiles.RunFiles(
        Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs", "example"
    )
    files.write("prompt", "example")
    data = request.to_dict(run_request())
    if case == "account":
        data["account"] = "other"
    if case == "profile":
        data.update(harness="claude", claude_profile="unknown", reason="example")
    path = tmp_path / "received.json"
    path.write_text("[]" if case == "list" else json.dumps(data))
    if case == "duplicate":
        files.write("request.json", json.dumps(data))
    if case in ("rc", "pgid"):
        files.write(case, "0")
    arguments = [
        "example",
        str(path),
        "--host",
        "unknown" if case == "host" else "example-linux",
    ]
    if case == "missing":
        arguments = ["example", "--host", "example-linux"]
    assert run_host.main(arguments, "example run-host") == 1
    missing_path = files.path("request.json")
    details = {
        "host": "host 'unknown' is not configured",
        "list": "request must be a JSON object",
        "duplicate": "run 'example' already exists",
        "rc": "run 'example' has already started",
        "pgid": "run 'example' has already started",
        "account": "account 'other' is not enabled on example-linux",
        "profile": "claude profile 'unknown' is not configured",
        "missing": f"[Errno 2] No such file or directory: '{missing_path}'",
    }
    assert capsys.readouterr().err == f"example run-host: {details[case]}\n"
    if case in ("duplicate", "rc", "pgid"):
        assert not files.path("log").exists()
        assert (
            files.path("rc").read_text() == "0"
            if case == "rc"
            else not files.path("rc").exists()
        )
    else:
        assert files.path("rc").read_text() == "1\n"
        assert files.path("log").read_text() == f"run refused: {details[case]}\n"


def tmux_environment_probe(root: Path, harness: Harness) -> None:
    run = make_run(
        root,
        harness=harness.value,
        timeout=900,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example" if harness is Harness.CLAUDE else None,
    )
    paths = preparation(run).paths
    env = run_host.policy.environment(
        run.settings, run.host, harness, paths, run.request.account
    )
    if harness is Harness.CLAUDE:
        env[credentials.CLAUDE_TOKEN_ENV] = "example-claude"
    command = ("/usr/bin/env",)
    child = process_group.launch(
        command,
        env=env,
        cwd=str(root),
        streams=(None, subprocess.PIPE, subprocess.PIPE),
        terminal=None,
    )
    output, error = child.communicate(timeout=5)
    assert child.returncode == 0, error
    assert dict(line.split("=", 1) for line in output.splitlines()) == env
    assert os.environ["EXAMPLE_PARENT_MARKER"] == "parent-marker"
    assert os.environ["EXAMPLE_TMUX_MARKER"] == "server-marker"
    (root / "result").write_text("isolated")


@pytest.mark.parametrize("harness", Harness)
def test_tmux_server_and_parent_markers_do_not_reach_a_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    operating_system = Os.MACOS if sys.platform == "darwin" else Os.LINUX
    with host_tools.configured(host_settings(), operating_system):
        try:
            tmux = host_tools.resolve("tmux")
        except Invalid:
            pytest.skip("tmux is unavailable in the production host search path")
    monkeypatch.setenv("EXAMPLE_PARENT_MARKER", "parent-marker")
    loader = (
        "import runpy, sys, subprocess, traceback\n"
        "from pathlib import Path\n"
        "try:\n"
        "    sys.path.insert(0, str(Path(sys.argv[1]).parent))\n"
        "    m=runpy.run_path(sys.argv[1])\n"
        f"    fn=m['{tmux_environment_probe.__name__}']\n"
        "    fn(Path(sys.argv[2]), m['Harness'](sys.argv[3]))\n"
        "except BaseException:\n"
        "    (Path(sys.argv[2]) / 'error').write_text(traceback.format_exc())\n"
        "finally:\n"
        "    subprocess.run([sys.argv[5], '-S', sys.argv[4], 'wait-for', '-S', "
        "'complete'], check=True)\n"
    )
    with tempfile.TemporaryDirectory(prefix="ag-tmux-") as directory:
        socket_path = str(Path(directory) / "socket")
        base = [tmux, "-S", socket_path]
        subprocess.run(
            [
                *base,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "anchor",
                "sleep",
                "30",
            ],
            check=True,
            timeout=5,
        )
        try:
            subprocess.run(
                [
                    *base,
                    "set-environment",
                    "-g",
                    "EXAMPLE_TMUX_MARKER",
                    "server-marker",
                ],
                check=True,
                timeout=5,
            )
            subprocess.run(
                [
                    *base,
                    "new-session",
                    "-d",
                    "-s",
                    "probe",
                    sys.executable,
                    "-c",
                    loader,
                    str(Path(__file__).resolve()),
                    str(tmp_path),
                    harness.value,
                    socket_path,
                    tmux,
                ],
                check=True,
                timeout=5,
            )
            subprocess.run([*base, "wait-for", "complete"], check=True, timeout=10)
            assert (tmp_path / "result").exists(), (tmp_path / "error").read_text()
            assert (tmp_path / "result").read_text() == "isolated"
        finally:
            subprocess.run([*base, "kill-server"], check=False, timeout=5)


@pytest.mark.parametrize("phase", ["fetch", "secret"])
def test_startup_deadline_stops_the_recorded_setup_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    run = make_run(tmp_path)
    _, clone = repository(run.home / "Code" / "github.com" / "example-org")
    clone.rename(clone.parent / "example-repo")
    run = dataclasses.replace(run, deadline=time.monotonic() + 5)
    original = run_host.worktree.git
    ready = tmp_path / "setup-child.json"

    def hang() -> str:
        code = (
            "import json, os, signal, sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(json.dumps([os.getpid(), os.getpgrp()])); "
            "signal.pause()"
        )
        return subprocess.check_output(
            [sys.executable, "-c", code, str(ready)], text=True
        )

    def git(path: Path, *args: str) -> str:
        return (
            hang() if phase == "fetch" and args[0] == "fetch" else original(path, *args)
        )

    def read(_reference: str) -> str:
        return hang()

    def boot() -> str:
        return "example-boot"

    monkeypatch.setattr(process_group, "boot_id", boot)
    monkeypatch.setattr(run_host.worktree, "git", git)
    if phase == "secret":
        prepared = preparation(run)
        monkeypatch.setattr(
            run_host.worktree, "create", constant(Path(prepared.paths.worktree))
        )
        monkeypatch.setattr(secrets, "read", read)
    assert run_host.execute(run, "example") == 124
    assert run.files.path("rc").read_text() == "124\n"
    assert not run.files.path("launched").exists()
    pid, group = json.loads(ready.read_text())
    assert json.loads(run.files.path("pgid").read_text())["pgid"] == group
    assert process_group.leader_start(pid) is None
    assert process_group.leader_start(group) is None


def test_review_finishes_without_a_signing_agent_or_auth_write_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(
        tmp_path, harness="agy", profile="review", commit="0" * 40, timeout=900
    )
    prepared = preparation(run)

    def prepare(*_args: object) -> run_host.Prepared:
        return prepared

    def harness(*_args: object) -> int:
        return 0

    monkeypatch.setattr(process_group, "prepare", prepare)
    monkeypatch.setattr(run_host, "run_harness", harness)
    run.files.receive(run.request)
    assert run_host.execute(run, "example") == 0
    assert run.files.path("rc").read_text() == "0\n"
    assert not (run.tmpdir / "ssh.sock").exists()
    assert not (run.home / ".codex").exists()
    assert runfiles.latest(run.files.root)["example"]["state"] == "ended"


@pytest.mark.parametrize("escalate", [False, True])
def test_hangup_stops_harness_group_writes_auth_and_rc(
    tmp_path: Path, escalate: bool
) -> None:
    script = r'''
import dataclasses, os, runpy, signal, subprocess, sys
from pathlib import Path
from tbhbdev.alpha.agent import host_tools, process_group, run_host, runfiles
host_tools.resolve = lambda *args: "/usr/bin/true"
host_tools.codex_proxy_supported = lambda *args: True

sys.path.insert(0, str(Path(sys.argv[1]).parent))
helpers = runpy.run_path(sys.argv[1])
run = helpers["make_run"](Path(sys.argv[2]))
run = dataclasses.replace(
    run, settings=dataclasses.replace(run.settings, kill_grace_seconds=0.2)
)
prepared = dataclasses.replace(
    helpers["preparation"](run), auth_before='{"value":"old"}'
)
source = runfiles.private_dir(run.home / ".codex") / "auth.json"
source.write_text('{"value":"old"}')
run_host._load = lambda args, tmpdir: (run, "example task")
process_group.prepare = lambda *args: prepared
process_group.boot_id = lambda: "example-boot"
original_launch = process_group.launch_owned
stub = """
import os, signal, sys
from pathlib import Path
child = os.fork()
if child == 0:
    signal.pause()
else:
    def finish(number, frame):
        os.waitpid(child, 0)
        sys.exit(0)
    signal.signal(signal.SIGTERM, finish)
    (Path(os.environ["CODEX_HOME"]) / "auth.json").write_text('{"value":"new"}')
    print(child, flush=True)
    signal.pause()
"""
if sys.argv[3] == "True":
    stub = """
import os, signal
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(Path(os.environ["CODEX_HOME"]) / "auth.json").write_text('{"value":"new"}')
print(os.getpid(), flush=True)
signal.pause()
"""
    original_stop = process_group.stop_started
    def stop(pid, grace):
        print("cleanup", flush=True)
        sys.stdin.readline()
        original_stop(pid, grace)
    process_group.stop_started = stop

def launch(spec, *, started):
    child = original_launch(
        dataclasses.replace(
            spec, command=(sys.executable, "-c", stub),
            streams=(None, subprocess.PIPE, None), terminal=None,
        ), started=started,
    )
    descendant = int(child.stdout.readline())
    (run.files.root / "descendant").write_text(str(descendant))
    return child
process_group.launch_owned = launch
write = runfiles.RunFiles.write
def recorded(self, suffix, text):
    write(self, suffix, text)
    if suffix == "phase" and text == "running":
        print("ready", flush=True)
runfiles.RunFiles.write = recorded
sys.exit(run_host.main(["example", "--host", "example-linux"], "example run-host"))
'''
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).resolve()),
            str(tmp_path),
            str(escalate),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert supervisor.stdout is not None
        assert supervisor.stdout.readline() == "ready\n"
        supervisor.send_signal(signal.SIGHUP)
        if escalate:
            assert supervisor.stdout.readline() == "cleanup\n"
            supervisor.send_signal(signal.SIGTERM)
            assert supervisor.stdin is not None
            supervisor.stdin.write("continue\n")
            supervisor.stdin.flush()
        output, error = supervisor.communicate(timeout=10)
        assert supervisor.returncode == 143, output + error
        files = runfiles.RunFiles(tmp_path / "runs", "example")
        assert files.path("rc").read_text() == "143\n"
        assert files.path("phase").read_text() == "ended"
        assert (
            tmp_path / "home" / ".codex" / "auth.json"
        ).read_text() == '{"value":"new"}'
        group = json.loads(files.path("pgid").read_text())["pgid"]
        descendant = int((files.root / "descendant").read_text())
        assert process_group.leader_start(group) is None
        assert process_group.leader_start(descendant) is None
        assert not process_group.alive(group)
    finally:
        group_file = tmp_path / "runs" / "example.pgid"
        if group_file.exists():
            recorded = Group(**json.loads(group_file.read_text()))
            if process_group.identity(recorded.pgid) == recorded:
                process_group.stop_started(recorded.pgid, 0.2)
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.communicate(timeout=10)


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("rc", [None, 0, 7])
def test_index_metadata_records_complete_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared: bool, rc: int | None
) -> None:
    run = make_run(
        tmp_path,
        harness="claude",
        claude_profile="primary",
        reason="example reason",
        **{"class": "example-class", "variant": "example-variant", "role": "worker"},
    )
    workspace = preparation(run)
    workspace = dataclasses.replace(
        workspace,
        delivery=credentials.Delivery(
            {}, None, gh_fallback=True, claude_profile="secondary"
        ),
    )
    monkeypatch.setattr(time, "time", lambda: 2000)
    assert run_host.index_entry(run, workspace if prepared else None, rc) == {
        "v": 1,
        "kind": "exec",
        "harness": "claude",
        "model": run.request.model,
        "account": "agent",
        "repo": run.request.repo,
        "class": "example-class",
        "variant": "example-variant",
        "reason": "example reason",
        "role": "worker",
        "unsandboxed": False,
        "authorized_by": None,
        "run": "example",
        "host": "example-linux",
        "worktree": workspace.paths.worktree if prepared else None,
        "started": 1000,
        "ended": None if rc is None else 2000,
        "rc": rc,
        "cleaned": None,
        "kept": False,
        "claude_profile": "secondary" if prepared else "primary",
        "trace_id": None,
        "state": "running" if rc is None else "ended",
        "gh_fallback": prepared,
    }


def test_host_defaults_use_fake_home_and_configured_machine(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file.write_text(EXAMPLE)
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(socket, "gethostname", lambda: "example-linux")
    root = Path.home() / ".local" / "state" / "tbhbdev" / "agent-runs"
    files = runfiles.RunFiles(root, "example")
    files.receive(run_request())
    files.write("prompt", "example task")

    def execute(run: run_host.Run, text: str) -> int:
        assert run.files.root == root
        assert run.host.name == "example-linux"
        assert text == "example task"
        return 37

    monkeypatch.setattr(run_host, "execute", execute)
    assert run_host.main(["example"], "example run-host") == 37


def test_host_help_names_program_and_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        run_host.main(["--help"], "example run-host")
    assert caught.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("usage: example run-host ")
    assert "--host HOST" in output.out
    assert "--prompt PROMPT" in output.out


def test_host_passes_configured_full_access_authorization(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file.write_text(EXAMPLE.replace("unsandboxed = false", "unsandboxed = true"))
    root = Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs"
    files = runfiles.RunFiles(root, "example")
    files.receive(
        run_request(
            unsandboxed=True,
            authorized_by="operator-chat-2026-11-09",
            reason="approval",
        )
    )
    files.write("prompt", "example task")

    def execute(run: run_host.Run, _text: str) -> int:
        assert run.request.unsandboxed is True
        assert run.request.authorized_by == "operator-chat-2026-11-09"
        return 37

    monkeypatch.setattr(run_host, "execute", execute)
    assert (
        run_host.main(["example", "--host", "example-linux"], "example run-host") == 37
    )


def check_harness_arguments(
    run: run_host.Run, prepared: run_host.Prepared, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, ...]]:
    harness, kind = run.request.harness, run.request.kind
    commands: list[tuple[str, ...]] = []
    real_build = run_host.policy.build
    real_argv = run_host.argv.harness

    def build(*args: object, **kwargs: object) -> Policy:
        assert args == (run.settings, run.host)
        assert kwargs.pop("protected")
        assert kwargs.pop("denied") == run_host.resolved_denies(run, prepared.paths)
        assert kwargs == {
            "harness": harness,
            "profile": run.request.profile,
            "account": run.request.account,
            "unsandboxed": False,
            "paths": prepared.paths,
        }
        return real_build(
            run.settings,
            run.host,
            harness=harness,
            profile=run.request.profile,
            account=run.request.account,
            unsandboxed=False,
            paths=prepared.paths,
        )

    def command(actual: RunRequest, value: Policy, **kwargs: object) -> tuple[str, ...]:
        project = (
            str(run.tmpdir / "agy-project.json") if harness is Harness.AGY else None
        )
        text = render.brief(kind, "example task")
        assert actual is run.request
        assert kwargs == {
            "worktree": prepared.paths.worktree,
            "brief_file": str(run.files.path("prompt")),
            "agy_project": project,
            "agy_brief": text,
            "schema_file": None,
            "schema_text": None,
        }
        generated = real_argv(
            actual,
            value,
            worktree=prepared.paths.worktree,
            brief_file=str(run.files.path("prompt")),
            agy_project=project,
            agy_brief=text,
        )

        commands.append(generated)
        return generated

    monkeypatch.setattr(run_host.policy, "build", build)
    monkeypatch.setattr(run_host.argv, "harness", command)
    return commands


def check_launch_contract(
    run: run_host.Run,
    prepared: run_host.Prepared,
    command: tuple[str, ...],
    kwargs: dict[str, object],
    original: tuple[str, ...],
) -> None:
    harness, kind = run.request.harness, run.request.kind
    original = ("/usr/bin/true", *original[1:])
    env = prepared.delivery.env
    assert not run.files.path("launched").exists()
    assert kwargs["cwd"] == prepared.paths.worktree
    expected = (
        run_host.policy.environment(
            run.settings, run.host, harness, prepared.paths, run.request.account
        )
        | env
    )
    assert kwargs["env"] == expected
    assert all(
        marker not in repr(command)
        for marker in (
            "example-parent-credential",
            "example-tmux-credential",
            "example-claude",
        )
    )
    assert kwargs["terminal"] == (None if kind is RunKind.EXEC else 0)
    if kind is RunKind.EXEC:
        streams = cast("tuple[TextIO, TextIO, TextIO]", kwargs["streams"])
        assert streams[0].name == str(run.files.path("prompt"))
        assert streams[0].read() == render.brief(kind, "example task")
        assert streams[1].name == str(run.files.path("out"))
        assert streams[1].mode == "w"
        assert streams[2].name == str(run.files.path("log"))
        assert streams[2].mode == "a"
    else:
        assert kwargs["streams"] == (None, None, None)
    if harness is Harness.AGY:
        project = json.loads((run.tmpdir / "agy-project.json").read_text())
        assert project["name"] == "agent-example"
        assert project["path"] == prepared.paths.worktree
        assert project["timeout"] == 900
        assert project["permissions"]
        assert command[command.index("--project") + 1] == str(
            run.tmpdir / "agy-project.json"
        )
    if kind is RunKind.EXEC and harness is Harness.CODEX:
        assert command[:-3] == original[:-1]
        assert command[-3:] == (
            "--output-last-message",
            str(run.tmpdir / "out.final"),
            "-",
        )
    if kind is not RunKind.EXEC or harness is not Harness.CODEX:
        assert command == original
        assert "--output-last-message" not in command


class HostRuntime:
    def __init__(
        self, run: run_host.Run, prepared: run_host.Prepared, failure: str | None
    ) -> None:
        self.run, self.prepared, self.failure = run, prepared, failure
        self.events: list[str] = []
        self.phases: list[str] = []
        self.agent = Child()
        self.write_file = run.files.write

    def prepare(
        self,
        operation: Callable[[], object],
        files: runfiles.RunFiles,
        deadline: float,
        grace: float,
    ) -> run_host.Prepared | str:
        assert callable(operation)
        assert files is self.run.files
        assert deadline == self.run.deadline
        assert grace == self.run.settings.kill_grace_seconds
        if self.failure == "result":
            return "invalid"
        if self.failure == "timeout":
            raise TimeoutError
        if self.failure == "interrupt":
            raise run_host.RunInterruptedError
        if self.failure == "invalid":
            message = "safe preparation error"
            raise Invalid(message)
        if self.failure == "os":
            message = "unprinted-error-material"
            raise OSError(message)
        return self.prepared

    def harness(
        self, run: run_host.Run, prepared: run_host.Prepared, stack: ExitStack
    ) -> int:
        assert run is self.run
        assert prepared is self.prepared
        assert isinstance(stack, ExitStack)
        self.events.append("group-stopped")
        return 7

    def sign(self, key: str, tmpdir: Path, deadline: float) -> Child:
        assert key == "example-key"
        assert tmpdir == self.run.tmpdir
        assert deadline == self.run.deadline
        self.events.append("agent-started")
        return self.agent

    def write_back(self, source: Path, destination: Path, before: str | None) -> None:
        assert source == self.run.home / ".codex" / "auth.json"
        assert destination == Path(self.prepared.paths.codex_home) / "auth.json"
        assert before == "example-before"
        self.events.append("auth")
        if self.failure == "auth":
            raise OSError

    def stop(self, agent: Child) -> None:
        assert agent is self.agent
        self.events.append("agent-stopped")

    def write(self, suffix: str, text: str) -> None:
        if suffix == "phase":
            self.phases.append(text)
        if suffix == "rc":
            assert self.phases[-1] == "ended"
            entry = runfiles.latest(self.run.files.root).get("example")
            assert entry is None or entry["rc"] is None
        self.write_file(suffix, text)

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(process_group, "prepare", self.prepare)
        monkeypatch.setattr(run_host, "run_harness", self.harness)
        monkeypatch.setattr(credentials, "signing_agent", self.sign)
        monkeypatch.setattr(credentials, "auth_write_back", self.write_back)
        monkeypatch.setattr(run_host, "stop_agent", self.stop)
        monkeypatch.setattr(self.run.files, "write", self.write)


def test_probe_failure_stops_its_recorded_harness_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_group, "boot_id", constant("example-boot"))
    real_start = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int,
        stderr: int | None = None,
        text: bool,
    ) -> subprocess.Popen[str]:
        assert text is True
        child = real_start(
            command, stdin=stdin, stdout=stdout, stderr=stderr, text=True
        )
        real_signal = child.send_signal

        def send(number: int) -> None:
            if number == signal.SIGHUP:
                message = "probe signal failed"
                raise RuntimeError(message)
            real_signal(number)

        monkeypatch.setattr(child, "send_signal", send)
        return child

    monkeypatch.setattr(subprocess, "Popen", start)
    with pytest.raises(RuntimeError, match=r"^probe signal failed$"):
        test_hangup_stops_harness_group_writes_auth_and_rc(tmp_path, escalate=False)
    files = runfiles.RunFiles(tmp_path / "runs", "example")
    group = json.loads(files.path("pgid").read_text())["pgid"]
    descendant = int((files.root / "descendant").read_text())
    assert not process_group.alive(group)
    assert process_group.leader_start(descendant) is None


def index_should_fail(failure: str, entry: dict[str, object]) -> bool:
    return (failure == "initial-index" and entry["rc"] is None) or (
        failure == "final-index" and entry["rc"] is not None
    )


@pytest.mark.parametrize("failure", ["initial-index", "final-index", "agent", "phase"])
def test_cleanup_failures_cannot_skip_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run = make_run(tmp_path)
    prepared = dataclasses.replace(
        preparation(run),
        delivery=credentials.Delivery(
            {}, "key", gh_fallback=False, claude_profile=None
        ),
    )
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(run_host, "run_harness", constant(7))
    monkeypatch.setattr(credentials, "signing_agent", constant(Child()))
    stopped: list[str] = []

    def stop(_child: object) -> None:
        stopped.append("stopped")
        if failure == "agent":
            raise OSError

    append = runfiles.append

    def index(root: Path, entry: dict[str, object]) -> None:
        if index_should_fail(failure, entry):
            if entry["rc"] is not None:
                assert run.files.path("rc").read_text() == "7\n"
            raise OSError
        append(root, entry)

    write = run.files.write

    def record(suffix: str, text: str) -> None:
        if failure == "phase" and text == "ended":
            raise OSError
        write(suffix, text)

    monkeypatch.setattr(run_host, "stop_agent", stop)
    monkeypatch.setattr(runfiles, "append", index)
    monkeypatch.setattr(run.files, "write", record)
    if failure in ("agent", "phase"):
        with pytest.raises(OSError, match=r"^$"):
            run_host.execute(run, "example")
    else:
        assert run_host.execute(run, "example") == (
            1 if failure == "initial-index" else 7
        )
    check_cleanup_result(run, stopped, failure)


def check_cleanup_result(run: run_host.Run, stopped: list[str], failure: str) -> None:
    assert run.files.path("rc").read_text() == (
        "1\n" if failure == "initial-index" else "7\n"
    )
    assert stopped == ([] if failure == "initial-index" else ["stopped"])
    if failure == "final-index":
        assert run.files.path("log").read_text() == "completion index append failed\n"


@pytest.mark.parametrize("name", ["../unsafe", ""])
def test_invalid_name_has_no_completion_location(
    tmp_path: Path, config_file: Path, name: str
) -> None:
    config_file.write_text(EXAMPLE)
    assert run_host.main([name, "--host", "example-linux"], "example") == 1
    assert not list(tmp_path.rglob("*.rc"))


def test_load_interrupt_is_recorded_before_handlers_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load(_args: object, _tmpdir: Path) -> None:
        assert signal.getsignal(signal.SIGHUP) is run_host.interrupt
        os.kill(os.getpid(), signal.SIGHUP)

    monkeypatch.setattr(run_host, "_load", load)
    previous = signal.getsignal(signal.SIGHUP)
    assert run_host.main(["example"], "example") == 143
    root = Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev" / "agent-runs"
    assert (root / "example.rc").read_text() == "143\n"
    assert (
        root / "example.log"
    ).read_text() == "run refused: interrupted during load\n"
    assert signal.getsignal(signal.SIGHUP) == previous


@pytest.mark.parametrize("terminal_lost", [False, True])
def test_signal_at_launch_handoff_records_and_stops_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_lost: bool
) -> None:
    run = make_run(tmp_path, kind="start" if terminal_lost else "exec")
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    child = Child()
    events: list[str] = []

    def launch(*_args: object, **kwargs: object) -> Child:
        cast("Callable[[Child], None]", kwargs["started"])(child)
        os.kill(os.getpid(), signal.SIGHUP)
        return child

    def record(files: runfiles.RunFiles, pid: int) -> None:
        assert pid == child.pid
        files.write("pgid", "recorded")
        events.append("recorded")

    def stop(pid: int, grace: float) -> None:
        assert (pid, grace) == (child.pid, 0)
        events.append("stopped")
        os.kill(os.getpid(), signal.SIGTERM)

    def foreground(_fd: int, _pid: int) -> None:
        raise OSError

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", record)
    monkeypatch.setattr(process_group, "stop_started", stop)
    monkeypatch.setattr(process_group, "foreground", foreground)
    with process_group.defer_signals():
        signal.signal(signal.SIGHUP, run_host.interrupt)
        signal.signal(signal.SIGTERM, run_host.interrupt)
        assert run_host.execute(run, "example") == 143
    assert events == ["recorded", "stopped"]
    assert child.waits == [None]
    assert run.files.path("rc").read_text() == "143\n"
    if terminal_lost:
        assert (
            run.files.path("log").read_text() == "terminal unavailable during cleanup\n"
        )


def test_auth_skip_reason_is_logged_without_changing_harness_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(run_host, "run_harness", constant(0))
    monkeypatch.setattr(
        credentials, "auth_write_back", constant("unsafe auth skipped\n")
    )
    assert run_host.execute(run, "example") == 0
    assert run.files.path("log").read_text() == "unsafe auth skipped\n"


def test_failed_launch_still_records_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    run.files.receive(run.request)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))

    def launch(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(process_group, "launch_owned", launch)
    assert run_host.execute(run, "example") == 1
    assert run.files.path("rc").read_text() == "1\n"
    assert not run.files.path("launched").exists()


def test_missing_name_exits_one_without_rc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_host.main([], "example") == 1
    assert not list(tmp_path.rglob("*.rc"))
    assert capsys.readouterr() == ("", "example: run name is required\n")


def test_terminal_setup_failure_keeps_registered_cleanup_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path, kind="start")
    prepared = preparation(run)
    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    child = Child()
    monkeypatch.setattr(subprocess, "Popen", constant(child))
    events: list[str] = []

    def record(files: runfiles.RunFiles, pid: int) -> None:
        assert pid == child.pid
        files.write("pgid", "registered")
        events.append("registered")

    def foreground(_fd: int, _pid: int) -> None:
        assert events[0] == "registered"
        raise OSError

    def stop(pid: int, _grace: float) -> None:
        assert pid == child.pid
        events.append("stopped")

    monkeypatch.setattr(process_group, "record", record)
    monkeypatch.setattr(process_group, "foreground", foreground)
    monkeypatch.setattr(process_group, "stop_started", stop)
    assert run_host.execute(run, "example") == 1
    assert events == ["registered", "stopped"]
    assert child.waits == [None]
    assert run.files.path("pgid").read_text() == "registered"
    assert run.files.path("rc").read_text() == "1\n"


@pytest.mark.parametrize("common", [False, True])
def test_planned_paths_ignore_account_git_directory_data(
    tmp_path: Path, common: bool
) -> None:
    run = make_run(tmp_path)
    clone = Path(run_host.repo.clone_path(run.home, run.request.repo))
    clone.mkdir(parents=True)
    gitdir = run.home / "gitdir"
    gitdir.mkdir()
    (clone / ".git").write_text(f"gitdir: {gitdir}\n")
    if common:
        (gitdir / "commondir").write_text("../shared\n")
    paths = run_host.planned_paths(run)
    expected = clone / ".worktrees" / "agent-example" / ".git"
    assert paths.git_common_dir == str(expected)
    assert paths == run_host.RunPaths(
        home=str(run.home),
        worktree=str(clone / ".worktrees" / "agent-example"),
        tmpdir=str(run.tmpdir),
        codex_home=str(run.tmpdir / "codex"),
        audit_dir=str(run.home / run.settings.sandbox.audit_dir),
        git_common_dir=str(expected),
        uid=os.getuid(),
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("opt_out", [False, True])
def test_preflight_version_and_named_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    opt_out: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        timeout=900,
        reason="test",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
    )
    sandbox = dataclasses.replace(
        run.settings.sandbox,
        codex_network_proxy_opt_out="test exception" if opt_out else None,
        agy_unsandboxed_opt_out="test exception" if opt_out else None,
    )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, sandbox=sandbox)
    )
    checks: list[tuple[str, Path]] = []

    def supported(executable: str, directory: Path) -> bool:
        assert executable == "/usr/bin/true"
        assert directory == run.tmpdir
        checks.append((executable, directory))
        return False

    monkeypatch.setattr(host_tools, "codex_proxy_supported", supported)
    if not opt_out and harness in (Harness.CODEX, Harness.AGY):
        with pytest.raises(Invalid) as caught:
            run_host.preflight(run)
        assert str(caught.value) == (
            "Codex network_proxy support required (minimum 0.164.0); "
            "upgrade or configure codex_network_proxy_opt_out"
            if harness is Harness.CODEX
            else (
                "agy has no OS sandbox; configure agy_unsandboxed_opt_out "
                "with a named reason"
            )
        )
    else:
        run_host.preflight(run)
        if opt_out and harness is not Harness.CLAUDE:
            assert (
                run.files.path("log").read_text()
                == f"{harness} sandbox opt-out: test exception\n"
            )

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        f"{harness} sandbox opt-out: test exception\n"
        if opt_out and harness is not Harness.CLAUDE
        else ""
    )

    assert checks == (
        [("/usr/bin/true", run.tmpdir)]
        if harness is Harness.CODEX and not opt_out
        else []
    )


def test_mac_host_commands_are_resolved_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    run = make_run(tmp_path)
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=Os.MACOS))
    calls: list[str] = []

    def resolve(name: str, _path: str | None = None) -> str:
        calls.append(name)
        return "/usr/bin/true"

    monkeypatch.setattr(host_tools, "resolve", resolve)
    run_host.checked_policy(run, preparation(run).paths)
    assert calls == ["codex", "git", "ssh-agent", "ssh-add", "sysctl", "ps"]


@pytest.mark.parametrize(
    "source", ["run directory", "symlink write root", "harness", "interpreter"]
)
def test_host_paths_refuse_before_any_host_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    run = make_run(tmp_path)
    paths = preparation(run).paths
    if source == "run directory":
        root = str(run.files.root)
    elif source == "symlink write root":
        link = run.home / "writable-link"
        link.symlink_to(run.files.root, target_is_directory=True)
        root = str(link)
    else:
        root = str(run.tmpdir)
        planted = run.tmpdir / "planted"
        planted.write_text(
            "#!/bin/sh\n"
            if source == "harness"
            else "#!" + str(run.tmpdir / "interpreter") + "\n"
        )
        if source == "interpreter":
            (run.tmpdir / "interpreter").write_bytes(b"binary")
            (run.tmpdir / "interpreter").chmod(0o700)
            planted = tmp_path / "trusted-script"
            planted.write_text("#!" + str(run.tmpdir / "interpreter") + "\n")
        original = host_tools.resolve

        def selected(name: str, path: str | None = None) -> str:
            return str(planted) if name == "codex" else original(name, path)

        monkeypatch.setattr(host_tools, "resolve", selected)
    if source in ("run directory", "symlink write root"):
        run = dataclasses.replace(
            run,
            settings=dataclasses.replace(
                run.settings,
                sandbox=dataclasses.replace(run.settings.sandbox, write_roots=(root,)),
            ),
        )
    with pytest.raises(Invalid, match=r"protected host path.*overlaps write root"):
        run_host.checked_policy(run, paths)


def test_final_answer_is_copied_only_after_child_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    run.files.write("prompt", "task")
    run.files.receive(run.request)
    prepared = preparation(run)
    child = Child()
    original_read = Path.read_text
    reads: list[Path] = []

    def read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if path == run.files.path("request.json"):
            reads.append(path)
        return original_read(path, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", read)
    marker = run.tmpdir / "executed"

    def launch(
        spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        assert spec.command[-2] == str(run.tmpdir / "out.final")
        assert not run.files.path("out.final").exists()
        (run.tmpdir / "out.final").write_text("answer")
        run.files.path("request.json").write_text(
            f"__import__('pathlib').Path({str(marker)!r}).write_text('escaped')"
        )
        started(child)
        return child

    def stop(*_args: object) -> None:
        assert not run.files.path("out.final").exists()

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", stop)
    with ExitStack() as stack:
        assert run_host.run_harness(run, prepared, stack) == 7
    assert run.files.path("out.final").read_text() == "answer"
    assert not marker.exists()

    assert reads == [run.files.path("request.json")]


@pytest.mark.parametrize(
    ("harness", "kind"),
    [
        (Harness.CLAUDE, RunKind.EXEC),
        (Harness.AGY, RunKind.EXEC),
        (Harness.CODEX, RunKind.START),
    ],
)
def test_only_headless_codex_can_supply_final_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, kind: RunKind
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        kind=kind.value,
        timeout=900,
        reason="test",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
    )
    run.files.write("prompt", "task")
    (run.tmpdir / "out.final").write_text("unused")
    child = Child()

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        started(child)
        return child

    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", constant(None))
    monkeypatch.setattr(process_group, "foreground", constant(None))
    with ExitStack() as stack:
        assert run_host.run_harness(run, preparation(run), stack) == 7
    assert not run.files.path("out.final").exists()


@pytest.mark.parametrize("fail", [False, True])
def test_execute_scopes_the_mac_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=Os.MACOS))
    before = host_tools.search()

    def execute(actual: run_host.Run, text: str, stack: ExitStack) -> int:
        assert actual is run
        assert text == "task"
        assert isinstance(stack, ExitStack)
        assert (
            host_tools.search()
            == "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        )
        if fail:
            raise ValueError
        return 7

    monkeypatch.setattr(run_host, "_execute", execute)
    if fail:
        with pytest.raises(ValueError, match=r"^$"):
            run_host.execute(run, "task")
    else:
        assert run_host.execute(run, "task") == 7
    assert host_tools.search() == before


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", ["build", "review"])
def test_checked_policy_supplies_all_protected_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    os: Os,
    harness: Harness,
    profile: str,
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        profile=profile,
        timeout=900,
        commit="0" * 40 if profile == "review" else None,
        reason="test",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
    )
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=os))
    directories = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
    names = ("git", "ssh-agent", "ssh-add")
    if os is Os.MACOS:
        directories += ("/opt/homebrew/bin", "/usr/local/bin")
        names += ("sysctl", "ps")
    installed = ("/trusted/prefix", "/trusted/python", "/trusted/entry")
    monkeypatch.setattr(host_tools, "installation", constant(installed))
    protections: list[tuple[str, ...]] = []
    configs: list[run_host.Settings] = []
    supplied: list[run_host.RunPaths] = []
    original = run_host.policy.protect_host

    def protect(
        configured: run_host.Settings,
        paths: run_host.RunPaths,
        protected: tuple[str, ...],
    ) -> None:
        protections.append(protected)
        configs.append(configured)
        supplied.append(paths)
        original(configured, paths, protected)

    lookups: list[tuple[str, str | None]] = []
    headers: list[tuple[str, str]] = []

    def resolve(name: str, path: str | None = None) -> str:
        lookups.append((name, path))
        return "/usr/bin/true"

    def executable_paths(executable: str, search: str) -> tuple[str, ...]:
        headers.append((executable, search))
        return (executable,)

    monkeypatch.setattr(run_host.policy, "protect_host", protect)
    monkeypatch.setattr(host_tools, "resolve", resolve)
    monkeypatch.setattr(host_tools, "executable_paths", executable_paths)
    real = tmp_path / "resolved"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    paths = run_host.RunPaths(
        str(run.home),
        str(alias / "worktree"),
        str(alias / "temp"),
        str(real / "temp" / "codex"),
        str(alias / "audit"),
        str(alias / "common"),
        uid=12345,
    )
    expected_paths = dataclasses.replace(
        paths,
        worktree=str(real / "worktree"),
        tmpdir=str(real / "temp"),
        audit_dir=str(real / "audit"),
        git_common_dir=str(real / "common"),
    )
    value, executable = run_host.checked_policy(run, paths)
    expected = (
        str(run.files.root.resolve()),
        *run_host.protected_home(run),
        *installed,
        *directories,
        *(str(Path(path).resolve()) for path in directories),
    )
    assert protections == [expected, expected, ("/usr/bin/true",) * (len(names) + 1)]
    assert supplied == [expected_paths] * 3
    for index in (0, 2):
        assert configs[index].sandbox.write_roots == tuple(
            str(run.home / p) for p in run.settings.sandbox.write_roots
        )
        assert configs[index].sandbox.agy.build_write == tuple(
            str(run.home / p) for p in run.settings.sandbox.agy.build_write
        )
    assert configs[1] is run.settings
    name = (
        "agy-agent" if harness is Harness.AGY and profile == "build" else harness.value
    )
    assert lookups == [
        (name, value.env["PATH"]),
        *((name, ":".join(directories)) for name in names),
    ]
    assert headers == [
        ("/usr/bin/true", value.env["PATH"]),
        *(("/usr/bin/true", ":".join(directories)) for _name in names),
    ]
    assert executable == "/usr/bin/true"


def test_main_defaults_to_the_current_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    def load(args: object, _tmpdir: Path) -> tuple[run_host.Run, str]:
        assert isinstance(args, run_host.argparse.Namespace)
        assert args.host == "example-linux"
        return run, "task"

    monkeypatch.setattr(socket, "gethostname", constant("example-linux"))
    monkeypatch.setattr(run_host, "_load", load)
    monkeypatch.setattr(run_host, "execute", constant(7))
    assert run_host.main(["example"], "example") == 7


def test_preparation_checks_paths_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    monkeypatch.setattr(
        run_host,
        "planned_paths",
        constant(
            dataclasses.replace(
                run_host.planned_paths(run), worktree=str(run.files.root)
            )
        ),
    )

    def create(*_args: object) -> None:
        pytest.fail("the discovered common directory must be checked first")

    monkeypatch.setattr(run_host.worktree, "create", create)
    with pytest.raises(Invalid, match=r"protected host path.*overlaps write root"):
        run_host.prepare_workspace(run)


def test_linux_denies_resolve_links_drop_missing_and_deduplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    target = tmp_path / "runtime"
    target.mkdir()
    socket = target / "socket"
    socket.touch()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    candidates = (str(alias / "socket"), str(socket), str(alias / "missing"))
    monkeypatch.setattr(run_host.policy, "denied_paths", constant(candidates))
    value, _executable = run_host.checked_policy(run, preparation(run).paths)
    assert value.deny == (str(socket),)
    assert isinstance(value.detail, CodexPolicy)
    assert value.detail.deny == (str(socket),)


@pytest.mark.parametrize("request_file", [False, True])
@pytest.mark.parametrize("marker", ["rc", "pgid"])
def test_refused_rerun_preserves_every_file(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_file: bool,
    marker: str,
) -> None:
    config_file.write_text(EXAMPLE)
    files = runfiles.RunFiles(
        Path(os.environ["XDG_STATE_HOME"]) / "tbhbdev/agent-runs", "example"
    )
    files.receive(run_request())
    files.write("prompt", "raw brief")
    files.write("log", "finished output\n")
    files.write(marker, "7\n")
    before = {p.name: p.read_bytes() for p in files.root.iterdir()}
    monkeypatch.setattr(run_host, "execute", constant(99))
    arguments = ["example", "--host", "example-linux"]
    if request_file:
        arguments.insert(1, str(files.path("request.json")))
    assert run_host.main(arguments, "example run-host") == 1
    assert {p.name: p.read_bytes() for p in files.root.iterdir()} == before


def test_empty_state_home_uses_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert run_host.main(["example"], "example run-host") == 1
    assert (
        Path.home() / ".local/state/tbhbdev/agent-runs/example.rc"
    ).read_text() == "1\n"


@pytest.mark.parametrize(
    "entry",
    [
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
        ".control",
        ".control/sockets",
    ],
)
def test_startup_and_socket_parents_are_protected(tmp_path: Path, entry: str) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sockets_dir=".control/sockets/private",
            sandbox=dataclasses.replace(run.settings.sandbox, write_roots=(entry,)),
        ),
    )
    with pytest.raises(Invalid, match="protected host path"):
        run_host.checked_policy(run, preparation(run).paths)


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_driver_schema_file_reaches_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        reason="test",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
    )
    schema = '{"type":"object","properties":{"answer":{"type":"string"}}}'
    run.files.write("schema.json", schema)
    run.files.write("prompt", "raw brief")
    prepared = preparation(run)
    commands: list[tuple[str, ...]] = []

    class Child:
        pid = 99123

        def wait(self, **_kwargs: object) -> int:
            return 0

    def launch(
        spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        commands.append(spec.command)
        child = Child()
        started(child)
        return child

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", constant(None))
    with ExitStack() as stack:
        assert run_host.run_harness(run, prepared, stack) == 0
    command = commands[0]
    if harness is Harness.CODEX:
        assert command[command.index("--output-schema") + 1] == str(
            run.files.path("schema.json")
        )
    else:
        assert command[command.index("--json-schema") + 1] == schema
        assert command[command.index("--output-format") + 1] == "json"


def test_full_access_opt_out_is_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(
        tmp_path,
        unsandboxed=True,
        authorized_by="example-org/example-repo#1",
        reason="isolated host",
    )
    run_host.preflight(run)
    notice = "codex sandbox opt-out: isolated test host\n"
    assert capsys.readouterr().err == notice
    assert run.files.path("log").read_text() == notice


@pytest.mark.parametrize("os_name", Os)
def test_resolved_denies_preserve_macos_aliases_and_empty_linux_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_name: Os
) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=os_name))
    missing = (str(tmp_path / "missing"),)
    monkeypatch.setattr(run_host.policy, "denied_paths", constant(missing))
    assert run_host.resolved_denies(run, preparation(run).paths) == (
        () if os_name is Os.LINUX else missing
    )


def test_startup_symlink_target_is_protected(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    target = tmp_path / "trusted-startup"
    target.touch()
    (run.home / ".zshrc").symlink_to(target)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox, write_roots=(str(target),)
            ),
        ),
    )
    with pytest.raises(Invalid, match="protected host path"):
        run_host.checked_policy(run, preparation(run).paths)


def test_protected_home_lists_startup_files_and_socket_ancestors(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings, sockets_dir=".control/sockets/private"
        ),
    )
    expected = (
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
        ".control/sockets",
        ".control",
    )
    assert run_host.protected_home(run) == tuple(str(run.home / p) for p in expected)


def test_resolved_denies_receive_host_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    paths = dataclasses.replace(preparation(run).paths, uid=24680)

    def candidates(
        configured: run_host.Settings, host: run_host.Host, home: str, uid: int
    ) -> tuple[str, ...]:
        assert configured is run.settings
        assert host is run.host
        assert home == paths.home
        assert uid == 24680
        return ()

    monkeypatch.setattr(run_host.policy, "denied_paths", candidates)
    assert run_host.resolved_denies(run, paths) == ()


def test_unproven_harness_stop_withholds_completion_and_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(
        run_host,
        "checked_policy",
        constant((run_host.checked_policy(run, prepared.paths)[0], "/example/harness")),
    )
    child = Child()

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        started(child)
        return child

    def denied(_pgid: int, _grace: float) -> None:
        raise PermissionError

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", denied)
    assert run_host.execute(run, "example") == 1
    assert run.files.path("phase").read_text() == "unknown"
    assert not run.files.path("rc").exists()
    assert run.files.path("log").read_text() == (
        "run group stop unproven; completion withheld\n"
    )
    assert not (run.home / ".codex" / "auth.json").exists()
