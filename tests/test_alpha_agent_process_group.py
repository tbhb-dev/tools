"""Process groups are exercised in isolated supervisors, including a real PTY."""

from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import signal
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from agent_host_example import constant

from tbhbdev.alpha.agent import process_group as groups
from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions.run_state import Group
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess


def probe(code: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_group_stop_reaps_a_forked_child() -> None:
    probe("""\
        import os, signal, subprocess, sys
        from tbhbdev.alpha.agent import process_group as g
        g.boot_id = lambda: "example-boot"

        stub = "\\n".join([
            "import os, signal, sys",
            "child = os.fork()",
            "if child == 0:",
            "    signal.pause()",
            "else:",
            "    def finish(number, frame):",
            "        os.waitpid(child, 0)",
            "        sys.exit(0)",
            "    signal.signal(signal.SIGTERM, finish)",
            "    print(child, flush=True)",
            "    signal.pause()",
        ])
        child = g.launch(
            (sys.executable, "-c", stub),
            env={},
            cwd="/",
            streams=(None, subprocess.PIPE, subprocess.PIPE),
            terminal=None,
        )
        try:
            descendant = int(child.stdout.readline())
            assert os.getsid(child.pid) == child.pid
            assert os.getpgid(child.pid) == child.pid
            assert g.identity(child.pid).pgid == child.pid
            assert g.alive(child.pid)
            g.stop_started(child.pid, 0.2, child.poll)
            child.communicate(timeout=5)
            assert child.returncode == 0
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("child survived")
            assert not g.alive(child.pid)
            assert g.identity(child.pid) is None
            g.stop_started(child.pid, 0)
        finally:
            if child.poll() is None:
                g.stop_started(child.pid, 0)
            child.communicate(timeout=5)
    """)


def test_group_stop_escalates() -> None:
    probe("""\
        import subprocess, sys
        from tbhbdev.alpha.agent import process_group as g

        child = g.launch(
            (
                sys.executable,
                "-c",
                (
                    "import signal; signal.signal(signal.SIGTERM, sig"
                    "nal.SIG_IGN); print('ready', flush=True); signal"
                    ".pause()"
                ),
            ),
            env={},
            cwd="/",
            streams=(None, subprocess.PIPE, None),
            terminal=None,
        )
        assert child.stdout.readline() == "ready\\n"
        g.stop_started(child.pid, 0.05, child.poll)
        assert child.wait(timeout=5) == -9
    """)


def test_interactive_harness_keeps_terminal_and_foreground_group() -> None:
    probe("""\
        import errno, json, os, pty, select, subprocess, sys
        from tbhbdev.alpha.agent import process_group as g

        pid, master = pty.fork()
        if pid == 0:
            child = g.launch(
                (
                    sys.executable,
                    "-c",
                    (
                        "import json, os; text=input(); print(json.dumps("
                        "[os.getpgrp(), os.tcgetpgrp(0), os.getsid(0), te"
                        "xt]), flush=True)"
                    ),
                ),
                env={},
                cwd="/",
                streams=(None, None, None),
                terminal=0,
            )
            os.write(1, b"parent-ready\\n")
            rc = child.wait(timeout=5)
            g.foreground(0, os.getpgrp())
            assert os.tcgetpgrp(0) == os.getpgrp()
            sys.exit(rc)
        try:
            output = b""
            while b"parent-ready" not in output:
                assert select.select([master], [], [], 5)[0]
                output += os.read(master, 4096)
            os.write(master, b"hello\\n")
            while True:
                assert select.select([master], [], [], 5)[0]
                try:
                    part = os.read(master, 4096)
                except OSError as exc:
                    assert exc.errno == errno.EIO
                    break
                if not part:
                    break
                output += part
            assert os.waitpid(pid, 0)[1] == 0, output
            rows = [
                json.loads(line)
                for line in output.decode().splitlines()
                if line.startswith("[")
            ]
            assert len(rows) == 1, output
            pgid, foreground, session, text = rows[0]
            assert pgid == foreground
            assert session == pid
            assert pgid != session
            assert text == "hello"
        finally:
            os.close(master)
    """)


def test_linux_identity_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    values = {
        "/proc/sys/kernel/random/boot_id": "boot-example\n",
        "/proc/23/stat": "23 (a name ) with parentheses) S "
        + " ".join(str(n) for n in range(4, 23)),
    }

    def read(path: Path) -> str:
        if str(path) not in values:
            raise FileNotFoundError
        return values[str(path)]

    monkeypatch.setattr(Path, "read_text", read)
    assert groups.boot_id() == "boot-example"
    assert groups.leader_start(23) == "22"
    assert groups.identity(23) == Group(23, "boot-example", "22")
    assert groups.leader_start(24) is None
    assert groups.identity(24) is None
    values["/proc/23/stat"] = values["/proc/23/stat"].replace(") S ", ") Z ")
    assert groups.leader_start(23) is None


@pytest.mark.parametrize("seconds", [42, 2**63 + 42])
@pytest.mark.parametrize(
    ("size", "status", "wanted"),
    [(136, 2, "42:17"), (0, 2, None), (135, 2, None), (136, 5, None)],
)
def test_mac_identity(
    size: int,
    status: int,
    wanted: str | None,
    seconds: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Library:
        def proc_pidinfo(
            self,
            pid: int,
            flavor: int,
            arg: int,
            buffer: ctypes.Array[ctypes.c_char],
            length: int,
        ) -> int:
            assert (pid, flavor, arg, length) == (23, 3, 0, 136)
            struct.pack_into("=I", buffer, 4, status)
            buffer[8] = b"\xff"
            struct.pack_into("=QQ", buffer, 120, seconds, 17)
            return size

    def library(path: str, *, use_errno: bool) -> Library:
        assert path == "/usr/lib/libproc.dylib"
        assert use_errno is True
        return Library()

    monkeypatch.setattr(ctypes, "CDLL", library)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert groups.leader_start(23) == (None if wanted is None else f"{seconds}:17")


def test_mac_boot_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def output(command: list[str], *, text: bool, timeout: int) -> str:
        assert command == [groups.host_tools.resolve("sysctl"), "-n", "kern.boottime"]
        assert text is True
        assert timeout == 5
        return " boot-example \n"

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "check_output", output)
    assert groups.boot_id() == "boot-example"


def test_record_missing_leader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_pid: int) -> None:
        return None

    monkeypatch.setattr(groups, "identity", missing)
    with pytest.raises(Invalid) as caught:
        groups.record(runfiles.RunFiles(tmp_path, "example"), 23)
    assert str(caught.value) == "run group leader exited before it could be recorded"


def test_foreground_restores_handler_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = signal.getsignal(signal.SIGTTOU)

    def fail(fd: int, group: int) -> None:
        assert signal.getsignal(signal.SIGTTOU) == signal.SIG_IGN
        assert (fd, group) == (4, 23)
        raise OSError

    monkeypatch.setattr(os, "tcsetpgrp", fail)
    with pytest.raises(OSError, match=r"^$"):
        groups.foreground(4, 23)
    assert signal.getsignal(signal.SIGTTOU) == before


def test_prepare_success_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(groups, "boot_id", constant("example-boot"))
    files = runfiles.RunFiles(tmp_path, "example")
    assert (
        groups.prepare(lambda: "prepared", files, time.monotonic() + 5, 0) == "prepared"
    )
    assert files.path("pgid").exists()

    def invalid() -> None:
        message = "safe preparation error"
        raise Invalid(message)

    with pytest.raises(Invalid, match=r"^safe preparation error$"):
        groups.prepare(invalid, files, time.monotonic() + 5, 0)

    def failed() -> None:
        message = "marker-credential-value"
        raise OSError(message)

    with pytest.raises(Invalid) as caught:
        groups.prepare(failed, files, time.monotonic() + 5, 0)
    assert (
        str(caught.value)
        == "run preparation failed; check the clone and credential references"
    )


def test_prepare_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(groups, "boot_id", constant("example-boot"))
    files = runfiles.RunFiles(tmp_path, "example")
    with pytest.raises(TimeoutError):
        groups.prepare(lambda: time.sleep(10), files, time.monotonic() + 0.1, 0)
    group = json.loads(files.path("pgid").read_text())["pgid"]
    assert not groups.alive(group)


@pytest.mark.parametrize("outcome", ["success", "invalid", "error"])
def test_setup_worker_protocol(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    events: list[object] = []

    class FakeConnection:
        def send(self, value: object) -> None:
            events.append(value)

        def recv(self) -> None:
            events.append("acknowledged")

        def close(self) -> None:
            events.append("closed")

    def set_group(pid: int, group: int) -> None:
        assert (pid, group) == (0, 0)
        events.append("group")

    def handler(number: int, value: object) -> None:
        assert number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        assert value == signal.SIG_DFL

    def operation() -> str:
        if outcome == "invalid":
            message = "safe error"
            raise Invalid(message)
        if outcome == "error":
            message = "unprinted-marker"
            raise OSError(message)
        return "prepared"

    monkeypatch.setattr(os, "setpgid", set_group)
    monkeypatch.setattr(signal, "signal", handler)
    groups.prepare_child(cast("Connection", FakeConnection()), operation)
    result = (
        (True, "prepared")
        if outcome == "success"
        else (False, "safe error" if outcome == "invalid" else None)
    )
    assert events == ["group", None, "acknowledged", result, "closed"]


@pytest.mark.parametrize(
    "outcome", ["ready-timeout", "result-timeout", "success", "invalid", "error"]
)
@pytest.mark.parametrize("remaining", [-1, 8])
@pytest.mark.parametrize("reaped", [False, True, "kill-denied", "killed"])
def test_setup_parent_deadline_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    remaining: int,
    reaped: bool | str,
) -> None:
    events: list[object] = []
    polls = iter([outcome != "ready-timeout", outcome != "result-timeout"])
    values = iter(
        [
            None,
            (
                outcome == "success",
                "prepared"
                if outcome == "success"
                else ("safe error" if outcome == "invalid" else None),
            ),
        ]
    )

    class Connection:
        def poll(self, timeout: float) -> bool:
            assert timeout == max(0, remaining)
            return next(polls)

        def recv(self) -> object:
            return next(values)

        def send(self, value: object) -> None:
            assert value is None

        def close(self) -> None:
            events.append("connection-closed")

    worker = SetupWorker(reaped, events)

    class Context:
        def pipe(self) -> tuple[Connection, Connection]:
            return Connection(), Connection()

        def process(self, **kwargs: object) -> SetupWorker:
            assert kwargs["target"] is groups.prepare_child
            return worker

        Pipe = pipe
        Process = process

    def context(method: str) -> Context:
        assert method == "fork"
        return Context()

    def identity(pid: int) -> Group:
        return Group(pid, "boot", "42")

    def stop(
        owned: runfiles.RunFiles,
        pid: int,
        grace: float,
        reap: Callable[[], object] | None = None,
    ) -> None:
        assert owned is files
        assert (pid, grace) == (23, 0.25)
        assert reap == worker.is_alive
        events.append("stopped")

    monkeypatch.setattr(multiprocessing, "get_context", context)
    monkeypatch.setattr(time, "monotonic", lambda: 2)
    monkeypatch.setattr(groups, "identity", identity)
    monkeypatch.setattr(groups, "stop_owned", stop)
    files = runfiles.RunFiles(tmp_path, "example")
    if outcome == "success":
        assert groups.prepare(lambda: None, files, 2 + remaining, 0.25) == "prepared"
    elif outcome.endswith("timeout"):
        with pytest.raises(TimeoutError):
            groups.prepare(lambda: None, files, 2 + remaining, 0.25)
    else:
        with pytest.raises(Invalid) as caught:
            groups.prepare(lambda: None, files, 2 + remaining, 0.25)
        assert str(caught.value) == setup_failure_message(outcome)
    assert events == setup_events(reaped, outcome)
    assert files.uncertain is (reaped == "kill-denied")
    assert files.path("pgid").exists() is (outcome != "ready-timeout")
    if outcome != "ready-timeout":
        assert "example.pgid" in {p.name for p in tmp_path.iterdir()}


def test_stop_absent_group_and_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def signal_group(pgid: int, number: int) -> None:
        assert pgid == 23
        calls.append(number)
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", signal_group)
    assert groups.alive(23) is False
    groups.stop_started(23, 0)
    assert calls == [0, signal.SIGTERM, 0]


@pytest.mark.parametrize("terminal", [None, 4])
def test_launch_assigns_group_streams_and_exact_environment(
    monkeypatch: pytest.MonkeyPatch, terminal: int | None
) -> None:
    events: list[object] = []
    environment = {"PATH": "/example/bin", "EXAMPLE": "value"}
    streams = (subprocess.PIPE, subprocess.DEVNULL, subprocess.STDOUT)

    class Child:
        pid = 23

    child = Child()

    def spawn(command: tuple[str, ...], **kwargs: object) -> Child:
        assert command == ("example", "argument")
        assert kwargs == {
            "env": environment,
            "cwd": "/example/worktree",
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "text": True,
            "start_new_session": terminal is None,
            "process_group": 0 if terminal is not None else None,
        }
        assert kwargs["env"] is environment
        events.append("spawn")
        return child

    def foreground(descriptor: int, group: int) -> None:
        events.append(("foreground", descriptor, group))

    def signal_group(group: int, number: int) -> None:
        events.append(("signal", group, number))

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(groups, "foreground", foreground)
    monkeypatch.setattr(os, "killpg", signal_group)
    assert (
        groups.launch(
            ("example", "argument"),
            env=environment,
            cwd="/example/worktree",
            streams=streams,
            terminal=terminal,
        )
        is child
    )
    assert events == (
        ["spawn"]
        if terminal is None
        else ["spawn", ("foreground", 4, 23), ("signal", 23, signal.SIGCONT)]
    )


@pytest.mark.parametrize("released", [False, True])
def test_stop_grace_deadline_and_signal_order(
    monkeypatch: pytest.MonkeyPatch, released: bool
) -> None:
    events: list[object] = []
    ticks = iter((10, 10.1, 10.5, 10.6))

    def clock() -> float:
        return next(ticks)

    def alive(group: int) -> bool:
        assert group == 23
        return signal.SIGKILL not in events and not (released and "sleep" in events)

    def pause(duration: float) -> None:
        assert duration == 0.02
        assert "sleep" not in events
        events.append("sleep")

    def signal_group(group: int, number: int) -> None:
        assert group == 23
        events.append(number)

    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(time, "sleep", pause)
    monkeypatch.setattr(groups, "alive", alive)
    monkeypatch.setattr(os, "killpg", signal_group)
    assert groups.stop_started(23, 0.5) is True
    assert events == [signal.SIGTERM, "sleep", *([] if released else [signal.SIGKILL])]


def test_existing_group_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def signal_group(group: int, number: int) -> None:
        assert (group, number) == (23, 0)

    monkeypatch.setattr(os, "killpg", signal_group)
    assert groups.alive(23) is True


@pytest.mark.parametrize("pid", [None, 23])
def test_worker_pid_requires_a_started_worker(pid: int | None) -> None:
    class Worker:
        def __init__(self) -> None:
            self.pid = pid

    worker = cast("BaseProcess", Worker())
    if pid is None:
        with pytest.raises(Invalid, match=r"^setup worker has not started$"):
            groups.worker_pid(worker)
    else:
        assert groups.worker_pid(worker) == 23


@pytest.mark.parametrize("permitted", [False, True])
def test_launch_foreground_failure_stops_created_group(
    monkeypatch: pytest.MonkeyPatch,
    permitted: bool,
) -> None:
    events: list[object] = []

    class Child:
        pid = 23

        def poll(self) -> None:
            return None

        def wait(self) -> None:
            events.append("wait")

    def foreground(_fd: int, _pid: int) -> None:
        raise OSError

    child = Child()
    monkeypatch.setattr(subprocess, "Popen", constant(child))
    monkeypatch.setattr(groups, "foreground", foreground)

    def stop(pid: int, grace: float, reap: Callable[[], object] | None = None) -> bool:
        assert reap == child.poll
        events.append((pid, grace))
        return permitted

    monkeypatch.setattr(groups, "stop_started", stop)
    with pytest.raises(OSError, match=r"^$"):
        groups.launch(
            ("example",), env={}, cwd="/example", streams=(None, None, None), terminal=0
        )
    assert events == [(23, 0), *(["wait"] if permitted else [])]


@pytest.mark.parametrize("reaped", [False, True])
def test_prepare_before_setpgid_is_bounded_and_never_signals_reaped_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped: bool
) -> None:
    def delayed(_connection: object, _operation: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)

    if reaped:

        def exited(_connection: object, _operation: object) -> None:
            return None

        monkeypatch.setattr(groups, "prepare_child", exited)
    else:
        monkeypatch.setattr(groups, "prepare_child", delayed)
    files = runfiles.RunFiles(tmp_path, "example")
    started = time.monotonic()
    with pytest.raises((TimeoutError, EOFError)):
        groups.prepare(lambda: None, files, started + 0.1, 0)
    assert time.monotonic() - started < 3


def test_deferred_ignored_signal_does_not_interrupt() -> None:
    with groups.ignore_signals(), groups.defer_signals():
        os.kill(os.getpid(), signal.SIGTERM)


def test_failed_spawn_has_no_group_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    def spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(OSError, match=r"^$"):
        groups.launch(
            ("example",), env={}, cwd="/", streams=(None, None, None), terminal=None
        )


@pytest.mark.parametrize("terminal", [None, 0])
def test_launch_transfers_the_exact_child_before_terminal_setup(
    monkeypatch: pytest.MonkeyPatch, terminal: int | None
) -> None:
    class Child:
        pid = 23

    child = Child()
    seen: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", constant(child))

    def foreground(_fd: int, _pid: int) -> None:
        assert seen == [child]
        raise OSError

    monkeypatch.setattr(groups, "foreground", foreground)
    spec = groups.Launch(("example",), {}, "/", (None, None, None), terminal)
    if terminal is None:
        assert groups.launch_owned(spec, started=seen.append) is child
    else:
        with pytest.raises(OSError, match=r"^$"):
            groups.launch_owned(spec, started=seen.append)
    assert seen == [child]


def test_cleanup_handler_installation_cannot_be_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = signal.signal
    previous = {s: signal.getsignal(s) for s in groups.INTERRUPTS}
    events: list[str] = []

    def unexpected(_number: int, _frame: object) -> None:
        assert groups.cleaning() is True

    def install(
        number: int, value: int | Callable[[int, object], None] | None
    ) -> object:
        result = original(number, value)
        if number == signal.SIGHUP and value == signal.SIG_IGN:
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    try:
        for number in previous:
            original(number, unexpected)
        monkeypatch.setattr(signal, "signal", install)
        with groups.ignore_signals():
            events.append("cleanup")
        assert events == ["cleanup"]
        assert groups.cleaning() is False
    finally:
        for number, handler in previous.items():
            original(number, handler)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_permission_denial_never_means_stopped(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    signals: list[int] = []

    def denied(_pgid: int, number: int) -> None:
        signals.append(number)
        raise PermissionError

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(time, "sleep", constant(None))
    ticks = iter((10, 11, 12, 13))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    assert groups.alive(23) is True
    assert groups.stop_started(23, 0) is False
    assert signal.SIGKILL in signals


def test_sigkill_must_be_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((10, 11, 12, 12.1, 12.2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(groups, "alive", constant(value=True))
    signals: list[tuple[int, int]] = []
    sleeps: list[float] = []

    def pause(duration: float) -> None:
        sleeps.append(duration)
        assert sleeps == [0.02]

    def send(pgid: int, number: int) -> None:
        signals.append((pgid, number))
        if number == signal.SIGKILL:
            raise PermissionError

    monkeypatch.setattr(os, "killpg", send)
    monkeypatch.setattr(time, "sleep", pause)
    assert groups.stop_started(23, 0) is False
    assert signals == [(23, signal.SIGTERM), (23, signal.SIGKILL)]
    assert sleeps == [0.02]


@pytest.mark.parametrize("error", [PermissionError, Invalid])
def test_unproven_stop_records_unknown_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    writes: list[tuple[str, str]] = []
    original = files.write

    def write(suffix: str, text: str) -> None:
        writes.append((suffix, text))
        original(suffix, text)

    monkeypatch.setattr(files, "write", write)

    def stop(pgid: int, grace: float, *_args: object) -> None:
        assert (pgid, grace) == (23, 0.25)
        raise error

    monkeypatch.setattr(groups, "stop_started", stop)
    assert groups.stop_owned(files, 23, 0.25) is False
    assert files.path("phase").read_text() == "unknown"
    assert {p.suffix for p in tmp_path.glob("example.*")} == {".phase", ".log"}
    assert not files.path("rc").exists()
    assert writes == [
        ("phase", "unknown"),
        ("log", "run group stop unproven; phase unknown\n"),
    ]


def test_reap_continues_after_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped: list[bool] = []
    signals: list[int] = []
    reports: list[str] = []

    def send(pgid: int, number: int) -> None:
        assert pgid == 23
        signals.append(number)
        if number == 0 and len(reaped) == 2:
            raise ProcessLookupError
        raise PermissionError

    def poll() -> None:
        reaped.append(True)

    monkeypatch.setattr(os, "killpg", send)
    ticks = iter((0, 0, 0, 0.2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    assert groups.stop_started(23, 0, poll, reports.append) is True
    assert signals == [signal.SIGTERM, 0, signal.SIGKILL, 0]
    assert reaped == [True, True]
    assert reports == [
        "permission denied sending SIGTERM to run group\n",
        "permission denied sending SIGKILL to run group\n",
    ]


def test_signal_errors_still_attempt_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[int] = []

    def failed(_pgid: int, number: int) -> None:
        signals.append(number)
        raise OSError

    monkeypatch.setattr(os, "killpg", failed)
    assert groups.stop_started(23, 0) is False
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_permitted_stop_reports_no_permission_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def send(pgid: int, number: int) -> None:
        assert pgid == 23
        if number == 0:
            raise ProcessLookupError
        assert number == signal.SIGTERM

    reports: list[str] = []
    monkeypatch.setattr(os, "killpg", send)
    assert groups.stop_started(23, 0, report=reports.append) is True
    assert reports == []


@pytest.mark.parametrize("stopped", [False, True])
def test_owned_stop_passes_reaping_and_appends_signal_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: bool
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    writes: list[tuple[str, str]] = []
    original = files.write

    def reap() -> None:
        return None

    def write(suffix: str, text: str) -> None:
        writes.append((suffix, text))
        original(suffix, text)

    def stop(
        pgid: int,
        grace: float,
        poll: Callable[[], object] | None = None,
        report: Callable[[str], None] | None = None,
    ) -> bool:
        assert (pgid, grace) == (23, 0.25)
        assert poll is reap
        assert report is not None
        report("first signal\n")
        report("second signal\n")
        return stopped

    monkeypatch.setattr(groups, "stop_started", stop)
    monkeypatch.setattr(files, "write", write)
    assert groups.stop_owned(files, 23, 0.25, reap) is stopped
    assert writes[:2] == [("log", "first signal\n"), ("log", "second signal\n")]
    assert files.path("log").read_text().startswith("first signal\nsecond signal\n")
    assert files.uncertain is (not stopped)


@pytest.mark.parametrize("failure", ["phase", "log"])
def test_unknown_record_errors_do_not_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    writes: list[str] = []

    def write(suffix: str, _text: str) -> None:
        writes.append(suffix)
        if suffix == failure:
            raise OSError

    monkeypatch.setattr(files, "write", write)
    groups.unknown(files)
    assert writes == ["phase", "log"]
    assert files.uncertain is True


def setup_failure_message(outcome: str) -> str:
    return (
        "safe error"
        if outcome == "invalid"
        else "run preparation failed; check the clone and credential references"
    )


def setup_events(reaped: bool | str, outcome: str) -> list[object]:
    return [
        "started",
        "connection-closed",
        "connection-closed",
        *([] if reaped is True or outcome == "ready-timeout" else ["stopped"]),
        ("joined", 0.25),
        *(["killed", ("joined", 0.25)] if reaped == "killed" else []),
        *([] if reaped == "kill-denied" else ["worker-closed"]),
    ]


def deny_worker_kill() -> None:
    raise PermissionError


class SetupWorker:
    pid = 23

    def __init__(self, reaped: bool | str, events: list[object]) -> None:
        self.reaped = reaped
        self.events = events
        self.exitcode: int | None = 0 if reaped is True else None

    def is_alive(self) -> bool:
        return self.reaped in ("kill-denied", "killed")

    def kill(self) -> None:
        if self.reaped == "kill-denied":
            deny_worker_kill()
        self.events.append("killed")

    def start(self) -> None:
        self.events.append("started")

    def join(self, timeout: float | None = None) -> None:
        assert timeout == 0.25
        self.events.append(("joined", timeout))

    def close(self) -> None:
        self.events.append("worker-closed")
