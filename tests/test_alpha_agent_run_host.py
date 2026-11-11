"""Host orchestration with fake credentials and disposable launch processes."""

from __future__ import annotations

import dataclasses
import grp
import hashlib
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from agent_host_example import (
    constant,
    host_settings,
    isolate_host_git,
    repository,
    run_request,
    trusted_host_stat,
)
from test_alpha_agent_codex_install import release
from test_alpha_agent_credentials import VALUES
from test_alpha_agent_settings import EXAMPLE

from tbhbdev import secrets
from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import (
    codex_install,
    credentials,
    host_tools,
    process_group,
    render,
    run_host,
    runfiles,
)
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.decisions.run_state import Group
from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    ClaudePolicy,
    CodexPolicy,
    Harness,
    Invalid,
    Os,
    RunKind,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from typing import TextIO

    from tbhbdev.alpha.agent.model import Policy, RunRequest, Settings


@pytest.fixture(autouse=True, params=["regular", "symlinked"])
def fake_host_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    directory = tmp_path / "host-bin"
    directory.mkdir()
    binary = directory / "multicall"
    binary.write_bytes(b"binary")
    binary.chmod(0o755)
    for name in ("true", "env"):
        path = directory / name
        if request.param == "symlinked":
            path.symlink_to(binary.name)
        else:
            path.write_bytes(b"binary")
            path.chmod(0o755)
    trusted_host_stat(monkeypatch)
    return directory


@pytest.fixture(autouse=True)
def fake_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_host_bin: Path
) -> None:
    def read(reference: str) -> str:
        return VALUES[reference]

    def installation(*_args: object) -> str:
        return "example-installation-credential"

    monkeypatch.setenv("HOME", str(runfiles.private_dir(tmp_path / "operator-home")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("CACHE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    isolate_host_git(tmp_path, monkeypatch)
    monkeypatch.setattr(
        run_host, "TAINT_DIRECTORY", tmp_path / "host-taint", raising=False
    )
    monkeypatch.setattr(secrets, "read", read)
    monkeypatch.setattr(credentials, "installation_token", installation)

    real_resolve = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        if follow_symlinks and name not in (
            "codex",
            "claude",
            "agy",
            "agy-agent",
            "prek",
        ):
            return real_resolve(name, path)
        if Path(name).is_absolute():
            return str(Path(name).resolve() if follow_symlinks else Path(name))
        return str(fake_host_bin / ("env" if name == "env" else "true"))

    monkeypatch.setattr(host_tools, "resolve", resolve)
    monkeypatch.setattr(host_tools, "codex_proxy_supported", constant(value=True))
    original_prepare = codex_install.prepare

    def prepare(executable: str, home: Path) -> tuple[str, tuple[str, ...]]:
        if executable == str(fake_host_bin / "true"):
            return executable, ()
        return original_prepare(executable, home)

    monkeypatch.setattr(codex_install, "prepare", prepare)


@pytest.fixture(autouse=True)
def portable_run_protocol(
    monkeypatch: pytest.MonkeyPatch, agent_suffix_case: None
) -> None:
    """Check protocol spelling even on a case-insensitive filesystem."""
    del agent_suffix_case
    original_copy = runfiles.copy_final

    def copy(source: Path, destination: Path) -> None:
        assert source.name == "out.final"
        assert destination.name.endswith(".out.final")
        original_copy(source, destination)

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
        str(runfiles.private_dir(run.tmpdir.parent / "codex")),
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
    (source.parent / "hooks.json").write_text('{"hooks":{"SessionStart":[]}}')
    copy = credentials.auth_copy

    def auth_copy(source_path: Path, destination: Path) -> str | None:
        assert source_path == run.home / ".codex" / "auth.json"
        assert destination == Path(run_host.planned_paths(run).codex_home) / "auth.json"
        return copy(source_path, destination)

    monkeypatch.setattr(credentials, "auth_copy", auth_copy)
    deliver = credentials.deliver

    def after_git_shape(
        request: RunRequest, settings: Settings, tmpdir: Path
    ) -> credentials.Delivery:
        workspace = clone / ".worktrees" / "agent-example"
        assert (workspace / "tracked").is_file()
        assert (workspace / ".git/hooks/pre-commit").is_file()
        assert (workspace / ".git/config.worktree").read_text() == ""
        assert (workspace / ".git/commondir").read_text() == ".\n"
        assert list((workspace / ".git/modules").iterdir()) == []
        return deliver(request, settings, tmpdir)

    monkeypatch.setattr(credentials, "deliver", after_git_shape)
    result = run_host.prepare_workspace(run)
    assert result.paths.uid == os.getuid()
    assert result.paths.home == str(run.home)
    assert result.paths.worktree == str(clone / ".worktrees" / "agent-example")
    assert result.paths.tmpdir == str(run.tmpdir)
    assert result.paths.codex_home == run_host.planned_paths(run).codex_home
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
        assert {p.name for p in copied_auth.parent.iterdir()} == {"auth.json"}


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

    def poll(self) -> int:
        return self.code

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

    def stop(pid: int, grace: float, *_args: object) -> None:
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

    def stop(pid: int, _grace: float, *_args: object) -> None:
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
        assert line["v"] == 2
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


@pytest.mark.parametrize("code", [0, 7], ids=["finished", "failed"])
def test_finished_run_writes_host_audit_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    _, workspace = repository(tmp_path / "repository")

    def harness(*_args: object) -> int:
        audit = Path(prepared.paths.audit_dir)
        (audit / "nested").mkdir(parents=True)
        (audit / "nested" / "event.json").write_bytes(b"event\n")
        (audit / "empty").write_bytes(b"")
        (audit / "link").symlink_to(workspace / "tracked")
        return code

    monkeypatch.setattr(run_host, "preflight", constant(None))
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(run_host, "run_harness", harness)
    assert run_host.execute(run, "example") == code
    summary = run.files.path("audit.json")
    assert summary.parent == run.files.path("rc").parent
    assert summary.stat().st_mode & 0o777 == 0o600
    assert json.loads(summary.read_text()) == {
        "rc": code,
        "files": {
            "empty": hashlib.sha256(b"").hexdigest(),
            "nested/event.json": hashlib.sha256(b"event\n").hexdigest(),
        },
    }


def test_failed_preparation_still_writes_host_audit_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    def refuse(_run: run_host.Run) -> None:
        message = "preflight refused"
        raise Invalid(message)

    monkeypatch.setattr(run_host, "preflight", refuse)
    assert run_host.execute(run, "example") == 1
    assert json.loads(run.files.path("audit.json").read_text()) == {
        "rc": 1,
        "files": {},
    }


def test_audit_summary_error_preserves_the_runs_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)

    def fail(*_args: object) -> None:
        raise OSError

    monkeypatch.setattr(run_host, "audit_summary", fail)
    assert run_host.finish(run, None, None, 7) == 7
    assert run.files.path("rc").read_text() == "7\n"
    assert run.files.path("log").read_text() == "audit summary write failed\n"


def test_interrupt_and_agent_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    with process_group.defer_signals(), pytest.raises(run_host.RunInterruptedError):
        run_host.interrupt(15, None)
    child = Child()
    stopped: list[int] = []

    def stop(pid: int, grace: float, reap: Callable[[], object] | None = None) -> bool:
        assert grace == 0
        assert reap == child.poll
        stopped.append(pid)
        return True

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
        assert run.tmpdir.name == "tools"
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
    env["PYTHONCOERCECLOCALE"] = "0"
    command = (
        sys.executable,
        "-c",
        (
            "import os; os.environ.pop('__CF_USER_TEXT_ENCODING', None); "
            'print("\\n".join(k + "=" + v for k, v in os.environ.items()))'
        ),
    )
    child = process_group.launch(
        command,
        env=env,
        cwd=str(root),
        streams=(None, subprocess.PIPE, subprocess.PIPE),
        terminal=None,
    )
    output, error = child.communicate(timeout=5)
    assert child.returncode == 0, error
    actual = dict(line.split("=", 1) for line in output.splitlines())
    assert actual.keys() == env.keys(), actual.keys() - env.keys()
    assert actual == env
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
    monkeypatch.setattr(
        run_host.worktree.repo, "ssh_url", constant(str(clone.parent / "origin"))
    )
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
            hang()
            if phase == "fetch" and args[0] in ("fetch", "ls-remote")
            else original(path, *args)
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


def test_refused_claude_review_never_starts_helpers_or_writes_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(
        tmp_path,
        harness="claude",
        profile="review",
        commit="0" * 40,
        timeout=900,
        claude_profile="primary",
        reason="example review",
    )

    def unexpected(*_args: object) -> None:
        pytest.fail("refused Claude must not start preparation or a harness")

    monkeypatch.setattr(process_group, "prepare", unexpected)
    monkeypatch.setattr(run_host, "run_harness", unexpected)
    run.files.receive(run.request)
    assert run_host.execute(run, "example") == 1
    assert run.files.path("rc").read_text() == "1\n"
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
binary = str(Path(sys.argv[2]) / "host-bin" / "true")
host_tools.resolve = lambda *args, **kwargs: binary
host_tools.executable_paths = lambda executable, *args, **kwargs: (executable,)
host_tools.codex_proxy_supported = lambda *args: True
run_host.codex_install.prepare = lambda executable, home: (executable, ())

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
    def stop(pid, grace, *args):
        print("cleanup", flush=True)
        sys.stdin.readline()
        return original_stop(pid, grace, *args)
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
            # The fixture records a fake boot ID; match the actual leader's start.
            if process_group.leader_start(recorded.pgid) == recorded.start:
                process_group.stop_started(recorded.pgid, 0.2)
        if supervisor.poll() is None:
            supervisor.terminate()
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
        "v": 2,
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
    text = "\n".join(
        line for line in EXAMPLE.splitlines() if not line.startswith("reviews_app_")
    )
    config_file.write_text(text.replace("unsandboxed = false", "unsandboxed = true"))
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
    original = (str(run.home.parent / "host-bin" / "true"), *original[1:])
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
    try:
        with pytest.raises(RuntimeError, match=r"^probe signal failed$"):
            test_hangup_stops_harness_group_writes_auth_and_rc(tmp_path, escalate=False)
    finally:
        files = runfiles.RunFiles(tmp_path / "runs", "example")
        group = json.loads(files.path("pgid").read_text())["pgid"]
        descendant = int((files.root / "descendant").read_text())
        deadline = time.monotonic() + 1
        while process_group.alive(group) and time.monotonic() < deadline:
            time.sleep(0.02)
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
    assert run_host.execute(run, "example") == (1 if failure == "initial-index" else 7)
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

    def stop(pid: int, grace: float, *_args: object) -> None:
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


def test_auth_skip_reason_is_logged_with_failure_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(run_host, "run_harness", constant(0))
    monkeypatch.setattr(
        credentials, "auth_write_back", constant("unsafe auth skipped\n")
    )
    runfiles.atomic(Path(prepared.paths.codex_home) / "auth.json", "{}")
    assert run_host.execute(run, "example") == 1
    assert "unsafe auth skipped" in run.files.path("log").read_text()


def test_refused_auth_is_moved_whole_to_private_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = preparation(run)
    source = runfiles.private_dir(run.home / ".codex") / "auth.json"
    runfiles.atomic(source, '{"tokens":{}}')
    prepared = dataclasses.replace(prepared, auth_before=source.read_text())
    target = Path(prepared.paths.codex_home) / "auth.json"
    refreshed = '{"tokens":{"access_token":"fixture-live-token"}}'
    original_datetime = run_host.datetime

    class FrozenClock:
        @staticmethod
        def now(zone: object) -> run_host.datetime:
            assert zone is run_host.UTC
            return original_datetime(2026, 1, 1, tzinfo=run_host.UTC)

    monkeypatch.setattr(run_host, "datetime", FrozenClock)
    mkdir = Path.mkdir
    names = iter(
        (
            "example-20260101T000000.000000Z",
            "example-20260101T000000.000000Z",
            "example-20260101T000000.000001Z",
        )
    )

    def checked_mkdir(
        path: Path, mode: int = 0o777, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path.parent == run.home / ".codex-recovery":
            assert path.name == next(names)
            assert mode == 0o700
            assert parents is False
            assert exist_ok is False
        mkdir(path, mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", checked_mkdir)
    expected_log = ""
    for _ in range(2):
        runfiles.private_dir(target.parent)
        runfiles.atomic(target, refreshed)
        inode = target.stat().st_ino
        assert run_host.finish(run, prepared, None, 0) == 1
        recovered = next(
            path
            for path in (run.home / ".codex-recovery").glob("*/auth.json")
            if path.stat().st_ino == inode
        )
        assert recovered.read_text() == refreshed
        assert stat.S_IMODE(recovered.stat().st_mode) == 0o600
        assert stat.S_IMODE(recovered.parent.stat().st_mode) == 0o700
        expected_log += (
            "codex auth write-back skipped: tokens.id_token: found missing; "
            f"expected string; recovery: {recovered}\n"
        )
        assert run.files.path("log").read_text() == expected_log
    assert len(list((run.home / ".codex-recovery").glob("*/auth.json"))) == 2
    assert {p.name for p in (run.home / ".codex-recovery").iterdir()} == {
        "example-20260101T000000.000000Z",
        "example-20260101T000000.000001Z",
    }
    assert source.read_text() == prepared.auth_before
    assert "fixture-live-token" not in run.files.path("log").read_text()
    paths = run_host.planned_paths(run)
    value, executable = run_host.checked_policy(run, paths)
    run.files.write("prompt", "fixture")
    command = run_host.harness_command(run, prepared, value, executable)
    override = next(arg for arg in command if arg.startswith("permissions.build="))
    filesystem = tomllib.loads(override)["permissions"]["build"]["filesystem"]
    assert filesystem[str(run.home / ".codex-recovery")] == "none"


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

    def stop(pid: int, _grace: float, *_args: object) -> None:
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
        codex_home=str(tmp_path / "state" / "tbhbdev" / "agent-codex" / run.files.name),
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
        assert executable == str(tmp_path / "host-bin" / "true")
        assert directory == run.tmpdir
        checks.append((executable, directory))
        return False

    monkeypatch.setattr(host_tools, "codex_proxy_supported", supported)
    if harness in {Harness.AGY, Harness.CLAUDE} or (
        not opt_out and harness is Harness.CODEX
    ):
        with pytest.raises(Invalid) as caught:
            run_host.preflight(run)
        assert str(caught.value) == preflight_failure_message(harness)
    else:
        run_host.preflight(run)
        if opt_out and harness is Harness.CODEX:
            assert (
                run.files.path("log").read_text()
                == f"{harness} sandbox opt-out: test exception\n"
            )

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        f"{harness} sandbox opt-out: test exception\n"
        if opt_out and harness is Harness.CODEX
        else ""
    )

    assert checks == (
        [(str(tmp_path / "host-bin" / "true"), run.tmpdir)]
        if harness is Harness.CODEX and not opt_out
        else []
    )


def preflight_failure_message(harness: Harness) -> str:
    if harness is Harness.CODEX:
        return (
            "Codex network_proxy support required (minimum 0.164.0); "
            "upgrade or configure codex_network_proxy_opt_out"
        )
    if harness is Harness.CLAUDE:
        return (
            "claude runs are refused: separate tool paths for Claude (H3) "
            "and the Claude credential decision remain open"
        )
    return "agy runs are refused: no OS sandbox"


def test_mac_host_commands_are_resolved_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    run = make_run(tmp_path)
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=Os.MACOS))
    calls: list[str] = []

    def resolve(
        name: str, _path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        assert follow_symlinks is False
        calls.append(name)
        return str(tmp_path / "host-bin" / "true")

    monkeypatch.setattr(host_tools, "resolve", resolve)
    run_host.checked_policy(run, preparation(run).paths)
    assert calls == [
        "codex",
        "git",
        "ssh-agent",
        "ssh-add",
        "ssh",
        "openssl",
        "sysctl",
        "ps",
        "security",
    ]


@pytest.mark.parametrize(
    "source", ["run directory", "symlink write root", "harness", "interpreter"]
)
def test_host_paths_refuse_before_any_host_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    run = make_run(tmp_path, harness="claude", claude_profile="primary", reason="test")
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
            "#!" + str(tmp_path / "host-bin" / "true") + "\n"
            if source == "harness"
            else "#!" + str(run.tmpdir / "interpreter") + "\n"
        )
        if source == "interpreter":
            (run.tmpdir / "interpreter").write_bytes(b"binary")
            (run.tmpdir / "interpreter").chmod(0o700)
            planted = tmp_path / "trusted-script"
            planted.write_text("#!" + str(run.tmpdir / "interpreter") + "\n")
        original = host_tools.resolve

        def selected(
            name: str, path: str | None = None, *, follow_symlinks: bool = True
        ) -> str:
            return (
                str(planted)
                if name == "claude"
                else original(name, path, follow_symlinks=follow_symlinks)
            )

        monkeypatch.setattr(host_tools, "resolve", selected)
    if source in ("run directory", "symlink write root"):
        run = dataclasses.replace(
            run,
            settings=dataclasses.replace(
                run.settings,
                sandbox=dataclasses.replace(run.settings.sandbox, write_roots=(root,)),
            ),
        )
    with pytest.raises(
        Invalid,
        match=(
            r"protected host path.*overlaps write root"
            r"|untrusted host executable component"
        ),
    ):
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

    def execute(
        actual: run_host.Run, text: str, stack: ExitStack, push_token: str | None = None
    ) -> int:
        assert push_token is None
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
    names = ("git", "ssh-agent", "ssh-add", "ssh", "openssl")
    if os is Os.MACOS:
        directories += ("/opt/homebrew/bin", "/usr/local/bin")
        names += ("sysctl", "ps", "security")
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

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        assert follow_symlinks is False
        lookups.append((name, path))
        return str(tmp_path / "host-bin" / "true")

    def executable_paths(
        executable: str, search: str, *, uid: int, write_roots: tuple[str, ...]
    ) -> tuple[str, ...]:
        assert (uid, write_roots) == (
            12345,
            (
                str(real / "codex"),
                str(real / "worktree"),
                str(real / "temp"),
                str(real / "audit"),
                "/dev/fd",
                *(
                    str((run.home / p).resolve())
                    for p in run.settings.sandbox.write_roots
                ),
                *(
                    str((run.home / p).resolve())
                    for p in run.settings.sandbox.agy.build_write
                ),
            ),
        )
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
        str(real / "codex"),
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
        str(Path(paths.codex_home).resolve()),
        *run_host.protected_home(run),
        *installed,
        *directories,
        *(str(Path(path).resolve()) for path in directories),
        *(
            str(run.home / path)
            for path in (
                run.settings.sandbox.path.linux
                if os is Os.LINUX
                else run.settings.sandbox.path.macos
            )
        ),
        *(
            str((run.home / path).resolve())
            for path in (
                run.settings.sandbox.path.linux
                if os is Os.LINUX
                else run.settings.sandbox.path.macos
            )
        ),
    )
    if harness is Harness.CODEX:
        assert protections.pop(1) == ()
        assert supplied.pop(1) == expected_paths
        assert configs.pop(1) == configs[0]
    assert protections == [
        (*expected, *run_host.full_denies(run, expected_paths)),
        expected,
        (str(tmp_path / "host-bin" / "true"),)
        * (len(names) + 1 + (harness is Harness.CODEX))
        + (
            ()
            if (tmp_path / "host-bin/true").is_symlink()
            else (str(tmp_path / "host-bin"),)
            * (len(names) + 1 + (harness is Harness.CODEX))
        ),
    ]
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
        *(
            [(str(tmp_path / "host-bin" / "true"), ":".join(directories))]
            if harness is Harness.CODEX
            else []
        ),
        (str(tmp_path / "host-bin" / "true"), value.env["PATH"]),
        *(
            (str(tmp_path / "host-bin" / "true"), ":".join(directories))
            for _name in names
        ),
    ]
    assert executable == str(tmp_path / "host-bin" / "true")


def assert_claude_marker_write_denies(
    detail: ClaudePolicy, markers: tuple[str, ...]
) -> None:
    sandbox = cast("dict[str, object]", detail.settings["sandbox"])
    filesystem = cast("dict[str, list[str]]", sandbox["filesystem"])
    permissions = cast("dict[str, list[str]]", detail.settings["permissions"])
    for path in markers:
        assert path in filesystem["denyWrite"]
        for tool in ("Edit", "Write"):
            assert f"{tool}(/{path})" in permissions["deny"]
            assert f"{tool}(/{path}/**)" in permissions["deny"]


@pytest.mark.parametrize("os_name", Os)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", ["build", "review"])
def test_host_marker_directory_is_write_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    os_name: Os,
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
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=os_name))
    resolved = tmp_path / "private" / "var"
    resolved.mkdir(parents=True)
    alias = tmp_path / "var"
    alias.symlink_to(resolved, target_is_directory=True)
    marker = alias / "tmp" / "tbhbdev-agent-taint"
    monkeypatch.setattr(run_host, "TAINT_DIRECTORY", marker)
    markers = (str(marker), str(resolved / "tmp" / "tbhbdev-agent-taint"))
    protected = run_host.protected_home(run)
    assert all(path in protected for path in markers)
    value, _executable = run_host.checked_policy(run, preparation(run).paths)
    match value.detail:
        case ClaudePolicy() as detail:
            assert_claude_marker_write_denies(detail, markers)
        case CodexPolicy() as detail:
            assert detail.read_roots[0] == ":root"
            assert all(
                not Path(path).is_relative_to(root)
                for path in markers
                for root in detail.write_roots
            )
        case AgyPolicy(grants):
            assert all(
                not Path(path).is_relative_to(grant.removeprefix("write_file(")[:-1])
                for path in markers
                for grant in grants
                if grant.startswith("write_file(")
            )


@pytest.mark.parametrize(
    "write_root",
    [Path("/var") / "tmp", Path("/var") / "tmp" / "tbhbdev-agent-taint"],
)
def test_host_marker_write_roots_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_root: Path
) -> None:
    run = make_run(tmp_path)
    marker = Path("/var") / "tmp" / "tbhbdev-agent-taint"
    monkeypatch.setattr(run_host, "TAINT_DIRECTORY", marker)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox, write_roots=(str(write_root),)
            ),
        ),
    )
    with pytest.raises(Invalid) as caught:
        run_host.checked_policy(run, preparation(run).paths)
    assert str(caught.value) == (
        f"protected host path {marker.resolve()} overlaps write root "
        f"{Path(write_root).resolve()}; narrow that write root"
    )


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
@pytest.mark.parametrize("marker", ["rc", "pgid", "owner"])
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
        ".config/mise",
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

        def poll(self) -> int:
            return 0

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


def test_full_access_is_available_behind_the_existing_gate(tmp_path: Path) -> None:
    run = make_run(
        tmp_path,
        unsandboxed=True,
        authorized_by="example-org/example-repo#1",
        reason="isolated host",
    )
    run_host.preflight(run)
    prepared = preparation(run)
    run.files.write("prompt", "probe")
    value, executable = run_host.checked_policy(run, prepared.paths)
    command = run_host.harness_command(run, prepared, value, executable)
    assert command[command.index("-s") + 1] == "danger-full-access"
    assert not any(arg.startswith("permissions.") for arg in command)


@pytest.mark.parametrize("profile", ["build", "review"])
@pytest.mark.parametrize("opt_out", [None, "isolated account"])
def test_agy_preflight_always_refuses(
    tmp_path: Path, profile: str, opt_out: str | None
) -> None:
    run = make_run(
        tmp_path,
        harness="agy",
        profile=profile,
        commit="0" * 40 if profile == "review" else None,
        timeout=900,
    )
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox, agy_unsandboxed_opt_out=opt_out
            ),
        ),
    )
    with pytest.raises(Invalid) as caught:
        run_host.preflight(run)
    assert str(caught.value) == "agy runs are refused: no OS sandbox"


@pytest.mark.parametrize("harness", [Harness.AGY])
def test_unconfined_run_is_refused_before_workspace_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    run = make_run(tmp_path, harness=harness.value, timeout=900)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox,
                agy_unsandboxed_opt_out=None,
            ),
        ),
    )

    def unexpected(*_args: object) -> None:
        pytest.fail("a run without its opt-out must not prepare or launch a workspace")

    monkeypatch.setattr(process_group, "prepare", unexpected)
    assert run_host.execute(run, "probe") == 1
    assert run.files.path("rc").read_text() == "1\n"
    assert run.files.path("log").read_text() == (
        "agy runs are refused: no OS sandbox\n"
    )


@pytest.mark.parametrize("os_name", Os)
def test_resolved_denies_preserve_macos_aliases_and_empty_linux_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_name: Os
) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=os_name))
    missing = (str(tmp_path / "missing"),)
    monkeypatch.setattr(run_host.policy, "denied_paths", constant(missing))
    assert run_host.resolved_denies(run, preparation(run).paths) == (
        ()
        if os_name is Os.LINUX
        else (
            *missing,
            str(run_host.worktree.mirror_path(run.home, run.request.repo)),
            str(run.home / ".codex-recovery"),
        )
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
        ".config/mise",
        ".ssh",
        ".claude",
        ".claude.json",
        ".codex",
        ".codex-recovery",
        ".control/sockets",
        ".control",
    )
    clone = Path(run_host.repo.clone_path(run.home, run.request.repo))
    mirror = run_host.worktree.mirror_path(run.home, run.request.repo)
    assert run_host.protected_home(run) == (
        *(str(run.home / p) for p in expected),
        str(clone / ".git"),
        str(mirror),
        str(mirror.parent),
        str(mirror.parent.parent),
        str(mirror.parent.parent.parent),
        str(run_host.userconfig.path()),
        str(run_host.TAINT_DIRECTORY),
    )


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


def test_unproven_harness_stop_keeps_rc_and_withholds_auth(
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

    def denied(_pgid: int, _grace: float, *_args: object) -> None:
        raise PermissionError

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", denied)
    assert run_host.execute(run, "example") == 7
    assert run.files.path("phase").read_text() == "unknown"
    assert run.files.path("rc").read_text() == "7\n"
    assert (
        run.files.path("log").read_text() == "run group stop unproven; phase unknown\n"
    )
    assert not (run.home / ".codex" / "auth.json").exists()


@pytest.mark.parametrize("relation", ["same", "parent", "child"])
def test_missing_linux_denies_are_checked_before_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relation: str
) -> None:
    run = make_run(tmp_path)
    denied = run.home / "missing" / "secret-store"
    root = {"same": denied, "parent": denied.parent, "child": denied / "inner"}[
        relation
    ]
    monkeypatch.setattr(run_host.policy, "denied_paths", constant((str(denied),)))
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(run.settings.sandbox, write_roots=(str(root),)),
        ),
    )
    assert run_host.resolved_denies(run, preparation(run).paths) == ()
    with pytest.raises(Invalid, match="protected host path"):
        run_host.checked_policy(run, preparation(run).paths)


def test_macos_denies_include_real_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=Os.MACOS))
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        run_host.policy, "denied_paths", constant((str(alias / "missing"),))
    )
    denied = run_host.resolved_denies(run, preparation(run).paths)
    assert str(alias / "missing") in denied
    assert str(target / "missing") in denied


@pytest.mark.parametrize(
    "entry",
    [
        ".ssh",
        ".claude",
        ".claude.json",
        ".codex",
        "clone",
        "mirror",
        "mirror-owner",
        "mirror-root",
        "config",
    ],
)
def test_host_state_cannot_be_a_configured_write_root(
    tmp_path: Path, entry: str
) -> None:
    run = make_run(tmp_path)
    clone = Path(run_host.repo.clone_path(run.home, run.request.repo))
    mirror = run_host.worktree.mirror_path(run.home, run.request.repo)
    roots = {
        "clone": clone / "other",
        "mirror": mirror,
        "mirror-owner": mirror.parent,
        "mirror-root": mirror.parent.parent,
        "config": run_host.userconfig.path(),
    }
    root = roots.get(entry, run.home / entry)
    if entry == "clone":
        (clone / ".git").mkdir(parents=True)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(run.settings.sandbox, write_roots=(str(root),)),
        ),
    )
    with pytest.raises(
        Invalid, match="main checkout" if entry == "clone" else "protected host path"
    ):
        run_host.checked_policy(run, preparation(run).paths)


def test_account_clone_must_not_use_an_external_common_directory(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    clone = Path(run_host.repo.clone_path(run.home, run.request.repo))
    clone.mkdir(parents=True)
    (clone / ".git").write_text(f"gitdir: {tmp_path / 'common'}\n")
    with pytest.raises(
        Invalid, match=r"^account clone must have its own git directory$"
    ):
        run_host.checked_policy(run, preparation(run).paths)


@pytest.mark.parametrize("os_name", Os)
@pytest.mark.parametrize("linked", [False, True])
def test_sandbox_path_must_be_outside_write_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_name: Os, linked: bool
) -> None:
    run = make_run(tmp_path)
    root = run.home / "writable"
    root.mkdir()
    entry = root / "bin"
    if linked:
        entry = run.home / "tools"
        entry.symlink_to(root, target_is_directory=True)
    sandbox = dataclasses.replace(
        run.settings.sandbox,
        write_roots=(str(root),),
        path=dataclasses.replace(
            run.settings.sandbox.path, linux=(str(entry),), macos=(str(entry),)
        ),
    )
    run = dataclasses.replace(
        run,
        host=dataclasses.replace(run.host, os=os_name),
        settings=dataclasses.replace(run.settings, sandbox=sandbox),
    )

    def lookup(*_args: object) -> str:
        pytest.fail("path overlap must be refused before executable inspection")

    monkeypatch.setattr(host_tools, "resolve", lookup)
    with pytest.raises(Invalid, match="protected host path"):
        run_host.checked_policy(run, preparation(run).paths)


def test_main_sets_host_path_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted"))

    def load(_args: object, _tmpdir: Path) -> None:
        assert os.environ["PATH"] == host_tools.search()
        message = "loading probe"
        raise Invalid(message)

    monkeypatch.setattr(run_host, "_load", load)
    assert run_host.main(["example"], "example") == 1


def test_late_temporary_cleanup_keeps_finished_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    original = tempfile.TemporaryDirectory

    @contextmanager
    def directory() -> Generator[str]:
        with original() as value:
            yield value
        raise OSError

    def execute(_run: run_host.Run, _text: str) -> int:
        run.files.write("log", "harness log\n")
        run.files.write("phase", "ended")
        run.files.write("rc", "7\n")
        return 7

    monkeypatch.setattr(tempfile, "TemporaryDirectory", directory)
    monkeypatch.setattr(run_host, "_files", constant(run.files))
    monkeypatch.setattr(run_host, "_load", constant((run, "task")))
    monkeypatch.setattr(run_host, "execute", execute)
    assert run_host.main(["example"], "example") == 7
    assert run.files.path("rc").read_text() == "7\n"
    assert run.files.path("phase").read_text() == "ended"
    assert run.files.path("log").read_text() == "harness log\n"


def test_signer_stop_denial_keeps_harness_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    child = Child()
    monkeypatch.setattr(process_group, "stop_started", constant(value=False))
    agent = cast("subprocess.Popen[str]", child)
    assert run_host.stop_agent(agent) is False
    assert child.waits == []
    assert run_host.finish(run, None, agent, 7) == 7
    assert run.files.uncertain is True
    assert run.files.path("phase").read_text() == "unknown"
    assert run.files.path("rc").read_text() == "7\n"


def test_terminal_cleanup_log_error_keeps_harness_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path, kind="start")
    prepared = preparation(run)
    child = Child()

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        started(child)
        return child

    def foreground(_fd: int, _pgid: int) -> None:
        raise OSError

    original = run.files.write

    def write(suffix: str, text: str) -> None:
        if suffix == "log":
            raise OSError
        original(suffix, text)

    monkeypatch.setattr(sys, "stdin", sys.__stdin__)
    monkeypatch.setattr(process_group, "prepare", constant(prepared))
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "stop_started", constant(None))
    monkeypatch.setattr(process_group, "foreground", foreground)
    monkeypatch.setattr(run.files, "write", write)
    assert run_host.execute(run, "task") == 7
    assert run.files.path("rc").read_text() == "7\n"


@pytest.mark.parametrize("failure", ["exited", "stopped", "wait-os", "wait-subprocess"])
def test_harness_cleanup_errors_keep_the_observed_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    run = make_run(tmp_path, timeout=60)
    prepared = preparation(run)
    run.files.write("prompt", "probe")
    child = Child()
    original = run.files.write
    stops: list[int] = []

    def launch(
        _spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        started(child)
        return child

    def wait(timeout: int | None = None) -> int:
        if timeout is None:
            if failure == "wait-os":
                raise OSError
            if failure == "wait-subprocess":
                raise subprocess.SubprocessError
        return 7

    def write(suffix: str, text: str) -> None:
        if suffix == "phase" and text == failure:
            raise OSError
        original(suffix, text)

    def stop(
        files: runfiles.RunFiles,
        pid: int,
        grace: float,
        reap: Callable[[], object] | None = None,
    ) -> bool:
        assert files is run.files
        assert (pid, grace) == (child.pid, run.settings.kill_grace_seconds)
        assert reap == child.poll
        stops.append(pid)
        return True

    monkeypatch.setattr(child, "wait", wait)
    monkeypatch.setattr(run.files, "write", write)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "stop_owned", stop)
    with ExitStack() as stack:
        assert run_host.run_harness(run, prepared, stack) == 7
    assert stops == [child.pid]


def test_mirror_protection_uses_the_run_home_without_xdg_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    monkeypatch.delenv("XDG_STATE_HOME")
    mirror = (
        run.home
        / ".local"
        / "state"
        / "tbhbdev"
        / "agent-mirrors"
        / "github.com"
        / "example-org"
        / "example-repo.git"
    )
    assert str(mirror) in run_host.protected_home(run)
    assert str(mirror) in run_host.full_denies(run, preparation(run).paths)


def test_checkout_validation_receives_aliases_and_all_write_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    clone = Path(run_host.repo.clone_path(run.home, run.request.repo))
    clone.parent.mkdir(parents=True)
    target = run.home / "account-clone"
    target.mkdir()
    clone.symlink_to(target, target_is_directory=True)
    calls: list[tuple[Path, Path, tuple[str, ...]]] = []
    original = run_host.worktree.validate_writes

    def validate(workspace: Path, source: Path, writes: tuple[str, ...]) -> None:
        calls.append((workspace, source, writes))
        original(workspace, source, writes)

    monkeypatch.setattr(run_host.worktree, "validate_writes", validate)
    paths = preparation(run).paths
    run_host.checked_policy(run, paths)
    sandbox = run.settings.sandbox
    writes = (
        paths.codex_home,
        paths.worktree,
        paths.tmpdir,
        paths.audit_dir,
        "/dev/fd",
        *(
            str((run.home / p).resolve())
            for p in (*sandbox.write_roots, *sandbox.agy.build_write)
        ),
    )
    assert calls == [(Path(paths.worktree), clone, writes)]
    assert clone.resolve() == target


@pytest.mark.parametrize("os_name", Os)
@pytest.mark.parametrize("profile", ["build", "review"])
def test_checked_policy_codex_table_has_unique_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_name: Os, profile: str
) -> None:
    run = make_run(
        tmp_path, profile=profile, commit="0" * 40 if profile == "review" else None
    )
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=os_name))
    mirror = run_host.worktree.mirror_path(run.home, run.request.repo)
    mirror.mkdir(parents=True)
    monkeypatch.setattr(
        host_tools, "resolve", constant(str(tmp_path / "host-bin" / "true"))
    )
    prepared = preparation(run)
    value, executable = run_host.checked_policy(run, prepared.paths)
    run.files.write("prompt", "probe")
    command = run_host.harness_command(run, prepared, value, executable)
    override = next(arg for arg in command if arg.startswith(f"permissions.{profile}="))
    table = tomllib.loads(override)["permissions"][profile]["filesystem"]
    assert isinstance(value.detail, CodexPolicy)
    expected = dict.fromkeys(value.detail.read_roots, "read")
    expected.update(dict.fromkeys(value.detail.write_roots, "write"))
    expected.update(dict.fromkeys(value.detail.deny, "none"))
    assert table == expected
    assert table[str(mirror)] == "none"
    if profile == "build":
        assert table[prepared.paths.worktree] == "write"


@pytest.mark.parametrize("profile", ["build", "review"])
@pytest.mark.parametrize("tainted", [False, True])
def test_preflight_refuses_review_on_tainted_host(
    tmp_path: Path, profile: str, tainted: bool
) -> None:
    run = make_run(
        tmp_path,
        profile=profile,
        commit="0" * 40 if profile == "review" else None,
        tainted_host=tainted,
    )
    if profile == "review" and tainted:
        with pytest.raises(Invalid) as caught:
            run_host.preflight(run)
        assert str(caught.value) == "review runs are refused on a tainted host"
    else:
        run_host.preflight(run)


@pytest.mark.parametrize(
    "missing", ["reviews_app_id", "reviews_app_client_id", "reviews_app_pem"]
)
def test_preflight_refuses_review_without_app_references(
    tmp_path: Path, missing: str
) -> None:
    run = make_run(tmp_path, profile="review", commit="0" * 40)
    original = run.settings.credentials[0]
    references = dataclasses.replace(
        original,
        reviews_app_id=None if missing == "reviews_app_id" else original.reviews_app_id,
        reviews_app_client_id=None
        if missing == "reviews_app_client_id"
        else original.reviews_app_client_id,
        reviews_app_pem=None
        if missing == "reviews_app_pem"
        else original.reviews_app_pem,
    )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, credentials=(references,))
    )
    with pytest.raises(Invalid) as caught:
        run_host.preflight(run)
    assert str(caught.value) == "reviews App credentials are not configured"


def test_preflight_checks_the_requested_accounts_app(tmp_path: Path) -> None:
    run = make_run(tmp_path, profile="review", commit="0" * 40)
    references = run.settings.credentials[0]
    other = dataclasses.replace(references, account="ops", reviews_app_id=None)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(run.settings, credentials=(other, references)),
    )
    run_host.preflight(run)


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_preflight_refuses_harness_without_recorded_pass(
    tmp_path: Path, harness: Harness
) -> None:
    run = make_run(
        tmp_path,
        harness=harness.value,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="sandbox test" if harness is Harness.CLAUDE else None,
    )
    run = dataclasses.replace(
        run, host=dataclasses.replace(run.host, proven_harnesses=())
    )
    with pytest.raises(Invalid) as caught:
        run_host.preflight(run)
    assert (
        str(caught.value)
        == f"{harness} has no recorded sandbox pass on {run.host.name}"
    )


@pytest.mark.parametrize("unsandboxed", [False, True])
@pytest.mark.parametrize("tainted", [False, True])
def test_preflight_announces_access_and_build_trust(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unsandboxed: bool,
    tainted: bool,
) -> None:
    run = make_run(
        tmp_path,
        unsandboxed=unsandboxed,
        authorized_by="example-org/example-repo#1" if unsandboxed else None,
        tainted_host=tainted,
    )
    run_host.preflight(run)
    lines = ["untrusted-host"] if tainted else []
    if unsandboxed:
        lines.append("unsandboxed full access authorized_by=example-org/example-repo#1")
    expected = "".join(f"{line}\n" for line in lines)
    assert capsys.readouterr().err == expected
    assert (run.files.path("log").read_text() if lines else "") == expected


@pytest.mark.parametrize("state", [None, "", "configured"])
def test_preflight_appends_durable_host_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    run = make_run(tmp_path, unsandboxed=True, authorized_by="operator-chat-2026-11-10")
    if state is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / state) if state else "")
    marker = run_host.TAINT_DIRECTORY / "agent.jsonl"
    marker.parent.mkdir(mode=0o1777)
    marker.parent.chmod(0o1777)
    marker.write_text('{"previous":true}\n')
    synced: list[int] = []
    original_sync = os.fsync
    original_open = os.open

    def opened(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == marker.parent:
            assert flags == os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            assert dir_fd is None
        else:
            assert path == marker.name
            assert flags == (
                os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            assert mode == 0o644
            assert dir_fd is not None
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def sync(fd: int) -> None:
        synced.append(fd)
        original_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(time, "time", lambda: 1234)
    run_host.preflight(run)
    lines = marker.read_text().splitlines()
    assert json.loads(lines[0]) == {"previous": True}
    assert json.loads(lines[1]) == {
        "v": 1,
        "run": "example",
        "account": "agent",
        "authorized_by": "operator-chat-2026-11-10",
        "at": 1234,
    }
    assert len(lines) == 2
    assert synced
    monkeypatch.setattr(os, "open", opened)
    run_host.mark_host(run)
    assert len(synced) == 3


def test_marker_failure_refuses_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(
        tmp_path, unsandboxed=True, authorized_by="example-org/example-repo#1"
    )

    original = os.fsync

    def sync(fd: int) -> None:
        marker = run_host.TAINT_DIRECTORY / "agent.jsonl"
        if marker.exists() and os.fstat(fd).st_ino == marker.stat().st_ino:
            raise OSError
        original(fd)

    def unexpected(*_args: object) -> None:
        pytest.fail("a failed host marker must refuse workspace preparation")

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(process_group, "prepare", unexpected)
    assert run_host.execute(run, "probe") == 1
    assert not run.files.path("launched").exists()


@pytest.mark.parametrize(
    "plant",
    [
        "fifo",
        "non-sticky",
        "directory-owner",
        "symlink",
        "hard-link",
        "file-owner",
        "directory-file",
    ],
)
def test_host_marker_refuses_unsafe_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: str
) -> None:
    run = make_run(tmp_path, unsandboxed=True, authorized_by="operator-chat-2026-11-10")
    directory = run_host.TAINT_DIRECTORY
    directory.mkdir()
    directory.chmod(0o1777)
    marker = directory / "agent.jsonl"
    if plant == "fifo":
        os.mkfifo(marker)
    elif plant == "non-sticky":
        directory.chmod(0o777)
    elif plant == "symlink":
        target = directory.with_name("real-directory")
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    elif plant == "hard-link":
        source = tmp_path / "source"
        source.write_text("unchanged")
        marker.hardlink_to(source)
    elif plant == "directory-file":
        marker.mkdir()
    original = os.fstat
    original_open = os.open

    def opened(
        path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == marker.name:
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def inspected(fd: int) -> os.stat_result:
        info = original(fd)
        if (plant == "directory-owner" and stat.S_ISDIR(info.st_mode)) or (
            plant == "file-owner" and stat.S_ISREG(info.st_mode)
        ):
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", inspected)
    monkeypatch.setattr(os, "open", opened)
    with pytest.raises((Invalid, OSError)) as caught:
        run_host.mark_host(run)
    if plant in ("non-sticky", "directory-owner"):
        assert str(caught.value) == (
            "host marker directory must be sticky and owned by root or this account"
        )
    elif plant in ("hard-link", "file-owner"):
        assert str(caught.value) == (
            "host marker must be a regular file owned by this account with one link"
        )
    if plant == "hard-link":
        assert (tmp_path / "source").read_text() == "unchanged"


def test_host_marker_directory_creation_ignores_umask(tmp_path: Path) -> None:
    run = make_run(tmp_path, unsandboxed=True, authorized_by="operator-chat-2026-11-10")
    previous = os.umask(0o077)
    try:
        run_host.mark_host(run)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(run_host.TAINT_DIRECTORY.stat().st_mode) == 0o1777
    assert json.loads((run_host.TAINT_DIRECTORY / "agent.jsonl").read_text())["v"] == 1


def test_host_marker_accepts_root_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    original = os.fstat

    def inspected(fd: int) -> os.stat_result:
        info = original(fd)
        if stat.S_ISDIR(info.st_mode):
            fields = list(info)
            fields[4] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", inspected)
    run_host.mark_host(run)
    assert (run_host.TAINT_DIRECTORY / "agent.jsonl").is_file()


@pytest.mark.parametrize("profile", ["build", "review"])
def test_codex_home_is_outside_all_write_roots(tmp_path: Path, profile: str) -> None:
    run = make_run(
        tmp_path, profile=profile, commit="0" * 40 if profile == "review" else None
    )
    paths = run_host.planned_paths(run)
    value, executable = run_host.checked_policy(run, paths)
    assert isinstance(value.detail, CodexPolicy)
    home = Path(paths.codex_home)
    assert home != run.home / ".codex"
    for root in value.detail.write_roots:
        assert not home.is_relative_to(root)
    run.files.write("prompt", "probe")
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    command = run_host.harness_command(run, prepared, value, executable)
    assert value.env["CODEX_HOME"] == str(home)
    assert f'shell_environment_policy.set.CODEX_HOME="{home}"' in command
    assert not (home / "hooks.json").exists()


def test_codex_home_refuses_configured_write_root(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    control = run.home / "control"
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox, write_roots=(str(control),)
            ),
        ),
    )
    paths = dataclasses.replace(run_host.planned_paths(run), codex_home=str(control))
    with pytest.raises(Invalid) as caught:
        run_host.checked_policy(run, paths)
    assert str(caught.value) == (
        f"protected host path {control} overlaps write root {control}; "
        "narrow that write root"
    )


@pytest.mark.parametrize("relative", [False, True])
def test_shell_config_directory_is_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    run = make_run(tmp_path)
    directory = run.home / "shell-config"
    monkeypatch.setenv("ZDOTDIR", "shell-config" if relative else str(directory))
    assert str(directory) in run_host.protected_home(run)


def test_protected_home_uses_the_account_home_without_state_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    monkeypatch.delenv("XDG_STATE_HOME")
    root = run.home / ".local" / "state" / "tbhbdev" / "agent-mirrors"
    assert str(root) in run_host.protected_home(run)


def test_resolved_executable_directory_cannot_contain_a_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    directory = runfiles.private_dir(run.home / "install")
    executable = directory / "codex"
    executable.write_bytes(b"binary")
    original = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        return (
            str(executable)
            if name == "codex"
            else original(name, path, follow_symlinks=follow_symlinks)
        )

    monkeypatch.setattr(host_tools, "resolve", resolve)
    root = directory / "cache"
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(run.settings.sandbox, write_roots=(str(root),)),
        ),
    )
    with pytest.raises(Invalid) as caught:
        run_host.checked_policy(run, run_host.planned_paths(run))
    assert str(caught.value) == f"standalone Codex install required: {executable}"


def test_non_standalone_codex_never_starts_a_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    executable = run.home / "codex"
    executable.write_bytes(b"fixture")
    original = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        return (
            str(executable)
            if name == "codex"
            else original(name, path, follow_symlinks=follow_symlinks)
        )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("non-standalone Codex must refuse before launch")

    monkeypatch.setattr(host_tools, "resolve", resolve)
    monkeypatch.setattr(process_group, "launch_owned", unexpected)
    monkeypatch.setattr(process_group, "prepare", unexpected)
    assert run_host.execute(run, "fixture") == 1
    assert not run.files.path("launched").exists()
    assert (
        f"standalone Codex install required: {executable}"
        in run.files.path("log").read_text()
    )


@pytest.mark.parametrize("os_name", Os)
@pytest.mark.parametrize("profile", ["build", "review"])
def test_codex_release_read_grant_keeps_account_state_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_name: Os, profile: str
) -> None:
    run = make_run(
        tmp_path, profile=profile, commit="0" * 40 if profile == "review" else None
    )
    release = (
        run.home / ".codex" / "packages" / "standalone" / "releases" / "example-version"
    )
    binary = release / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    (release / "codex-package.json").write_text('{"entrypoint":"bin/codex"}')
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    directory = runfiles.private_dir(run.home / ".local" / "bin")
    alias = directory / "codex"
    alias.symlink_to(binary)
    original = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        return (
            str(binary)
            if name == "codex"
            else original(name, path, follow_symlinks=follow_symlinks)
        )

    monkeypatch.setattr(host_tools, "resolve", resolve)
    run = dataclasses.replace(
        run,
        host=dataclasses.replace(run.host, os=os_name),
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox,
                denied_home_paths=(".codex",),
                host_path=(str(directory),),
            ),
        ),
    )
    paths = run_host.planned_paths(run)
    value, executable = run_host.checked_policy(run, paths)
    run.files.write("prompt", "probe")
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    command = run_host.harness_command(run, prepared, value, executable)
    override = next(arg for arg in command if arg.startswith(f"permissions.{profile}="))
    filesystem = tomllib.loads(override)["permissions"][profile]["filesystem"]
    copy = run.home / ".local/share/tbhbdev/codex" / release.name
    assert command[0] == str(copy / "bin/codex")
    assert filesystem[str(copy)] == "read"
    assert str(binary) not in filesystem
    assert str(alias) not in filesystem
    assert str(release) not in filesystem
    account_state = run.home / ".codex"
    assert filesystem[str(account_state)] == "none"
    for name in ("auth.json", "config.toml", "sessions"):
        path = account_state / name
        matches = [
            key for key in filesystem if key != ":root" and path.is_relative_to(key)
        ]
        assert filesystem[max(matches, key=len)] == "none"


@pytest.mark.parametrize("source", ["host", "tool"])
def test_mise_shim_install_targets_cannot_be_a_write_root(
    tmp_path: Path, source: str
) -> None:
    run = make_run(tmp_path)
    directory = run.home / ".local" / "share" / "mise" / "shims"
    installs = directory.parent / "installs"
    sandbox = dataclasses.replace(
        run.settings.sandbox,
        write_roots=(str(installs),),
        agy=dataclasses.replace(run.settings.sandbox.agy, build_write=()),
    )
    if source == "host":
        sandbox = dataclasses.replace(sandbox, host_path=(str(directory),))
    else:
        sandbox = dataclasses.replace(
            sandbox, path=dataclasses.replace(sandbox.path, linux=(str(directory),))
        )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, sandbox=sandbox)
    )
    with pytest.raises(Invalid) as caught:
        run_host.checked_policy(run, run_host.planned_paths(run))
    assert str(caught.value) == (
        f"protected host path {installs} overlaps write root {installs}; "
        "narrow that write root"
    )


@pytest.mark.parametrize("os_name", [Os.LINUX, Os.MACOS])
def test_system_temp_write_root_launches_with_state_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_name: Os
) -> None:
    run = make_run(tmp_path)
    system_temp = runfiles.private_dir(tmp_path / "system-temp")
    run = dataclasses.replace(
        run,
        tmpdir=runfiles.private_dir(system_temp / "tools"),
        host=dataclasses.replace(run.host, os=os_name),
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox, write_roots=(str(system_temp),)
            ),
        ),
    )
    paths = run_host.planned_paths(run)
    run_host.preflight(run)
    value, executable = run_host.checked_policy(run, paths)
    assert paths.codex_home == str(
        tmp_path / "state" / "tbhbdev" / "agent-codex" / run.files.name
    )
    assert value.env["CODEX_HOME"] == paths.codex_home
    run.files.write("prompt", "fixture")
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    command = run_host.harness_command(run, prepared, value, executable)
    assert command[0] == str(tmp_path / "host-bin" / "true")
    child = Child(code=0)

    def launch(
        spec: process_group.Launch, *, started: Callable[[Child], None]
    ) -> Child:
        assert spec.command == command
        assert spec.env["CODEX_HOME"] == paths.codex_home
        started(child)
        return child

    monkeypatch.setattr(process_group, "launch_owned", launch)
    monkeypatch.setattr(process_group, "record", constant(None))
    monkeypatch.setattr(process_group, "stop_started", constant(None))
    with ExitStack() as stack:
        assert run_host.run_harness(run, prepared, stack) == 0
    assert run.files.path("launched").exists()


@pytest.mark.parametrize("failure", [False, True])
def test_finish_removes_private_state_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    run = make_run(tmp_path)
    workspace = runfiles.private_dir(run.home / "worktree")
    monkeypatch.setattr(run_host.worktree, "create", constant(workspace))
    prepared = run_host.prepare_workspace(run)
    directory = Path(prepared.paths.codex_home)
    assert directory == tmp_path / "state" / "tbhbdev" / "agent-codex" / run.files.name
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def write_back(_source: Path, destination: Path, _before: str | None) -> None:
        assert destination.parent == directory
        assert directory.is_dir()
        if failure:
            raise OSError

    monkeypatch.setattr(credentials, "auth_write_back", write_back)
    assert run_host.finish(run, prepared, None, 0) == int(failure)
    assert directory.exists() is failure


@pytest.mark.parametrize("state", [None, ""])
def test_codex_home_defaults_to_account_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    run = make_run(tmp_path)
    if state is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", state)
    assert run_host.planned_paths(run).codex_home == str(
        run.home / ".local/state/tbhbdev/agent-codex/example"
    )


@pytest.mark.parametrize("profile", ["build", "review"])
def test_claude_preflight_names_both_open_conditions(
    tmp_path: Path, profile: str
) -> None:
    run = make_run(
        tmp_path,
        harness="claude",
        profile=profile,
        commit="0" * 40 if profile == "review" else None,
        claude_profile="primary",
        reason="phase refusal",
    )
    with pytest.raises(Invalid) as caught:
        run_host.preflight(run)
    assert str(caught.value) == (
        "claude runs are refused: separate tool paths for Claude (H3) "
        "and the Claude credential decision remain open"
    )


@pytest.mark.parametrize("root", ["parent", "self", "child"])
def test_codex_copy_refuses_overlapping_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    run = make_run(tmp_path)
    source = release(run.home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    original = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        return (
            str(source / "bin/codex")
            if name == "codex"
            else original(name, path, follow_symlinks=follow_symlinks)
        )

    monkeypatch.setattr(host_tools, "resolve", resolve)
    copy = run.home / ".local/share/tbhbdev/codex" / source.name
    write = {"parent": copy.parent, "self": copy, "child": copy / "cache"}[root]
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox, write_roots=(str(write),)
            ),
        ),
    )
    with pytest.raises(Invalid, match=r"protected host path.*example-version"):
        run_host.checked_policy(run, run_host.planned_paths(run))


def test_developer_git_paths_are_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = make_run(tmp_path)
    paths = preparation(run).paths
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=Os.MACOS))
    git = tmp_path / "host-bin/git"
    git.write_bytes(b"binary")
    git.chmod(0o755)
    original_resolve = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        return (
            str(git)
            if name == "git"
            else original_resolve(name, path, follow_symlinks=follow_symlinks)
        )

    monkeypatch.setattr(host_tools, "resolve", resolve)
    developer = str(tmp_path / "Developer/usr/bin/git")
    Path(developer).parent.mkdir(parents=True)
    Path(developer).write_bytes(b"binary")
    protections: list[tuple[str, ...]] = []
    checked: list[str] = []
    original = run_host.policy.protect_host

    def protect(
        settings: run_host.Settings,
        paths: run_host.RunPaths,
        protected: tuple[str, ...],
    ) -> None:
        protections.append(protected)
        original(settings, paths, protected)

    def git_paths(
        executable: str, operating_system: Os, uid: int, write_roots: tuple[str, ...]
    ) -> tuple[str, ...]:
        assert executable == str(git)
        assert operating_system is Os.MACOS
        assert uid == paths.uid
        assert paths.worktree in write_roots
        assert paths.tmpdir in write_roots
        assert paths.audit_dir in write_roots
        checked.append(executable)
        return (developer,)

    monkeypatch.setattr(host_tools, "git_shim_paths", git_paths, raising=False)
    monkeypatch.setattr(run_host.policy, "protect_host", protect)
    run_host.checked_policy(run, paths)
    assert checked == [str(git)]
    assert developer in protections[-1]
    assert str(Path(developer).parent) in protections[-1]


@pytest.mark.parametrize("through_link", [False, True])
def test_executable_chain_through_private_codex_home_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    through_link: bool,
) -> None:
    run = make_run(tmp_path)
    paths = preparation(run).paths
    directory = Path(paths.codex_home)
    planted = directory / "git"
    planted.write_bytes(b"binary")
    planted.chmod(0o755)
    selected = planted
    if through_link:
        selected = tmp_path / "git-link"
        selected.symlink_to(planted)
    original = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        return (
            str(selected)
            if name == "git"
            else original(name, path, follow_symlinks=follow_symlinks)
        )

    monkeypatch.setattr(host_tools, "resolve", resolve)
    with pytest.raises(Invalid, match="untrusted host executable component"):
        run_host.checked_policy(run, paths)


@pytest.fixture
def selected_xcode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> run_host.Run:
    """Model Apple's selection links without consulting the host filesystem."""
    run = make_run(tmp_path)
    run = dataclasses.replace(run, host=dataclasses.replace(run.host, os=Os.MACOS))
    links = {
        Path("/var"): Path("private/var"),
        Path("/var/db/xcode_select_link"): Path(
            "/Applications/Xcode.app/Contents/Developer"
        ),
        Path("/private/var/db/xcode_select_link"): Path(
            "/Applications/Xcode.app/Contents/Developer"
        ),
        Path("/root-alias"): Path("/"),
    }
    original_resolve = Path.resolve
    original_link = Path.is_symlink
    original_readlink = Path.readlink
    virtual = ("/var", "/private", "/Applications", "/usr", "/tool", "/root-alias")

    def resolve(path: Path, *, strict: bool = False) -> Path:
        if not any(path.is_relative_to(prefix) for prefix in virtual):
            return original_resolve(path, strict=strict)
        for link, target in links.items():
            if path.is_relative_to(link):
                return resolve(
                    link.parent / target / path.relative_to(link), strict=strict
                )
        return path

    def is_symlink(path: Path) -> bool:
        if any(path.is_relative_to(prefix) for prefix in virtual):
            return path in links
        return original_link(path)

    def readlink(path: Path) -> Path:
        if path in links:
            return links[path]
        return original_readlink(path)

    def lstat(path: Path) -> os.stat_result:
        mode = stat.S_IFLNK if is_symlink(path) else stat.S_IFDIR
        return os.stat_result((mode | 0o775, 0, 0, 1, 0, 80, 0, 0, 0, 0))

    def command(name: str, *_args: object, **_kwargs: object) -> str:
        return f"/usr/bin/{name}"

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(Path, "readlink", readlink)
    monkeypatch.setattr(host_tools, "lstat", lstat)
    monkeypatch.setattr(host_tools.sys, "platform", "darwin")
    monkeypatch.setattr(
        grp, "getgrgid", constant(grp.struct_group(("admin", "*", 80, [])))
    )
    monkeypatch.setattr(host_tools, "resolve", command)
    monkeypatch.setattr(host_tools, "executable_header", constant(b"binary"))
    monkeypatch.setattr(codex_install, "prepare", constant(("/usr/bin/codex", ())))
    return run


def test_checked_policy_xcode_protected_tuple(
    selected_xcode: run_host.Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    protections: list[tuple[str, ...]] = []

    def protect(
        _settings: Settings, _paths: run_host.RunPaths, protected: tuple[str, ...]
    ) -> None:
        protections.append(protected)

    monkeypatch.setattr(run_host.policy, "protect_host", protect)
    run_host.checked_policy(selected_xcode, preparation(selected_xcode).paths)
    assert protections[-1] == (
        "/var/db/xcode_select_link",
        "/var",
        "/private/var/db/xcode_select_link",
        "/Applications/Xcode.app/Contents/Developer",
        "/Applications/Xcode.app/Contents/Developer/usr/bin/git",
        "/usr/bin/codex",
        "/usr/bin/codex",
        "/usr/bin/git",
        "/usr/bin/ssh-agent",
        "/usr/bin/ssh-add",
        "/usr/bin/ssh",
        "/usr/bin/openssl",
        "/usr/bin/sysctl",
        "/usr/bin/ps",
        "/usr/bin/security",
        "/Applications/Xcode.app/Contents",
        "/Applications/Xcode.app/Contents/Developer/usr/bin",
        *("/usr/bin",) * 10,
    )
    assert all("/" not in protected for protected in protections)


def test_checked_policy_allows_selected_xcode(selected_xcode: run_host.Run) -> None:
    value, executable = run_host.checked_policy(
        selected_xcode, preparation(selected_xcode).paths
    )
    assert executable == "/usr/bin/codex"
    assert isinstance(value.detail, CodexPolicy)


@pytest.mark.parametrize("executable", ["/tool", "/root-alias/tool"])
def test_checked_policy_never_adds_a_parent_resolving_to_root(
    selected_xcode: run_host.Run, monkeypatch: pytest.MonkeyPatch, executable: str
) -> None:
    monkeypatch.setattr(codex_install, "prepare", constant((executable, ())))
    monkeypatch.setattr(host_tools, "git_shim_paths", constant(()))
    assert run_host.executable_parents((executable,)) == ()
    run_host.checked_policy(selected_xcode, preparation(selected_xcode).paths)


@pytest.mark.parametrize("kind", ["executable", "link", "parent"])
def test_checked_policy_still_refuses_executable_link_and_parent_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    run = make_run(tmp_path)
    entry = str(tmp_path / "tools" / "binary")
    link = str(tmp_path / "links" / "alias")
    write, protected = {
        "executable": (entry, entry),
        "link": (link + "/cache", link),
        "parent": (str(tmp_path / "tools" / "cache"), str(tmp_path / "tools")),
    }[kind]
    paths = dataclasses.replace(preparation(run).paths, worktree=write)
    monkeypatch.setattr(host_tools, "git_shim_paths", constant((entry, link)))
    monkeypatch.setattr(host_tools, "executable_links", constant((link,)))
    monkeypatch.setattr(host_tools, "executable_header", constant(b"binary"))
    with pytest.raises(Invalid) as caught:
        run_host.checked_policy(run, paths)
    assert str(caught.value) == (
        f"protected host path {protected} overlaps write root {write}; "
        "narrow that write root"
    )


def test_checked_policy_refuses_workspace_in_main_checkout(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    clone = run.home / "other-clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    paths = dataclasses.replace(preparation(run).paths, worktree=str(clone))
    with pytest.raises(Invalid, match="main checkout"):
        run_host.checked_policy(run, paths)


def test_checked_policy_allows_configured_workspace_subdirectory(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path)
    paths = run_host.planned_paths(run)
    workspace = Path(paths.worktree)
    (workspace / ".git").mkdir(parents=True)
    git_dir = workspace.parent.parent / ".git"
    git_dir.mkdir()
    sandbox = dataclasses.replace(
        run.settings.sandbox, write_roots=(str(workspace / "src"),)
    )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, sandbox=sandbox)
    )
    value, _ = run_host.checked_policy(run, paths)
    assert isinstance(value.detail, CodexPolicy)
    assert str(workspace / "src") in value.detail.write_roots


def test_checked_policy_checks_effective_hooks_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    paths = run_host.planned_paths(run)
    workspace = Path(paths.worktree)
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    def output(command: list[str], **kwargs: object) -> str:
        assert command[-1] == "core.hooksPath"
        assert kwargs["cwd"] == workspace
        return f"{run.tmpdir}/hooks\0/dev/null\0"

    monkeypatch.setattr(subprocess, "check_output", output)
    with pytest.raises(Invalid, match="under write root"):
        run_host.checked_policy(run, paths)


def test_main_passes_in_memory_push_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    synthetic = "synthetic-push-value"
    monkeypatch.setattr(run_host, "_load", constant((run, "task")))

    def execute(actual: run_host.Run, text: str, push_token: str | None = None) -> int:
        assert actual is run
        assert text == "task"
        assert push_token == synthetic
        return 4

    monkeypatch.setattr(run_host, "execute", execute)
    assert run_host.main(["example"], "run-host", push_token=synthetic) == 4


def test_push_failure_reaches_durable_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_run(tmp_path)
    prepared = dataclasses.replace(preparation(run), auth_before="example-before")
    runtime = HostRuntime(run, prepared, None)
    runtime.patch(monkeypatch)
    synthetic = "synthetic-push-value"

    def publish(
        actual: run_host.Run,
        ready: run_host.Prepared,
        stack: ExitStack,
        harness: object,
        token: str | None,
    ) -> int:
        assert actual is run
        assert ready == prepared
        assert isinstance(stack, ExitStack)
        assert harness == run_host.run_harness
        assert token == synthetic
        return 4

    monkeypatch.setattr(run_host.push, "run", publish)
    assert run_host.execute(run, "example", synthetic) == 4
    assert run.files.path("rc").read_text() == "4\n"
    assert run.files.path("phase").read_text() == "ended"
    assert runfiles.latest(run.files.root)["example"]["rc"] == 4


@pytest.mark.parametrize("claimed", [False, True])
def test_main_claims_owner_before_loading(
    monkeypatch: pytest.MonkeyPatch,
    claimed: bool,
) -> None:
    files = runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev/agent-runs", "example"
    )

    if claimed:
        files.write("owner", "host\n")
    calls: list[bool] = []

    def load(_args: object, _temporary: Path) -> tuple[run_host.Run, str]:
        assert files.path("owner").read_text() == "host\n"
        assert files.path("owner").stat().st_mode & 0o777 == 0o600
        calls.append(True)
        message = "test refusal"
        raise Invalid(message)

    monkeypatch.setattr(run_host, "_load", load)
    assert run_host.main(["example"], "run-host", claimed=claimed) == 1
    assert calls == [True]
