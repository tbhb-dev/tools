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
from tbhbdev.alpha.agent.decisions.run_state import Action, Group
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
            g.stop_started(child.pid, 0.2)
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
        g.stop_started(child.pid, 0.05)
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


@pytest.mark.parametrize("change", ["boot", "start", "missing", "rc", "owned"])
def test_recorded_group_ownership(tmp_path: Path, change: str) -> None:
    probe(
        """\
        import dataclasses, json, signal, subprocess, sys
        from pathlib import Path
        from tbhbdev.alpha.agent import process_group as g, runfiles
        from tbhbdev.alpha.agent.decisions.run_state import Action
        g.boot_id = lambda: "example-boot"

        files = runfiles.RunFiles(Path(sys.argv[1]), "example")
        child = g.launch(
            (
                sys.executable,
                "-c",
                ("import signal; print('ready', flush=True); signal.pause()"),
            ),
            env={},
            cwd="/",
            streams=(None, subprocess.PIPE, None),
            terminal=None,
        )
        try:
            assert child.stdout.readline() == "ready\\n"
            g.record(files, child.pid)
            recorded = json.loads(files.path("pgid").read_text())
            assert set(recorded) == {"pgid", "boot", "start"}
            change = sys.argv[2]
            if change in ("boot", "start"):
                recorded[change] = "different"
                files.write("pgid", json.dumps(recorded))
            elif change == "missing":
                g.stop_started(child.pid, 0)
                child.wait(timeout=5)
            elif change == "rc":
                files.write("rc", "0")
            result = g.stop_recorded(files, 0)
            if change == "owned":
                assert result is Action.STOP
                child.wait(timeout=5)
            elif change == "rc":
                assert result is Action.DONE
                assert child.poll() is None
            else:
                assert result is Action.UNKNOWN
                assert files.path("phase").read_text() == "unknown"
                if change != "missing":
                    assert child.poll() is None
        finally:
            g.stop_started(child.pid, 0)
            child.communicate(timeout=5)
    """,
        str(tmp_path),
        change,
    )


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


@pytest.mark.parametrize(
    "current", [None, Group(23, "other-boot", "42"), Group(23, "boot", "43")]
)
def test_stale_group_never_gets_a_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: Group | None
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    files.write("pgid", json.dumps({"pgid": 23, "boot": "boot", "start": "42"}))

    def identity(pgid: int) -> Group | None:
        assert pgid == 23
        return current

    stopped: list[int] = []
    writes: list[tuple[str, str]] = []

    def write(suffix: str, text: str) -> None:
        writes.append((suffix, text))

    def stop(pgid: int, _grace: float) -> None:
        stopped.append(pgid)

    monkeypatch.setattr(groups, "identity", identity)
    monkeypatch.setattr(groups, "stop_started", stop)
    monkeypatch.setattr(files, "write", write)
    assert groups.stop_recorded(files, 0) is Action.UNKNOWN
    assert writes == [("phase", "unknown")]
    assert stopped == []


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
@pytest.mark.parametrize("reaped", [False, True])
def test_setup_parent_deadline_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    remaining: int,
    reaped: bool,
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

    class Worker:
        pid = 23
        exitcode: int | None = 0 if reaped else None

        is_alive = staticmethod(lambda: False)

        def start(self) -> None:
            events.append("started")

        def join(self, timeout: float | None = None) -> None:
            events.append(("joined", timeout))

        def close(self) -> None:
            events.append("worker-closed")

    class Context:
        def pipe(self) -> tuple[Connection, Connection]:
            return Connection(), Connection()

        def process(self, **kwargs: object) -> Worker:
            assert kwargs["target"] is groups.prepare_child
            return Worker()

        Pipe = pipe
        Process = process

    def context(method: str) -> Context:
        assert method == "fork"
        return Context()

    def identity(pid: int) -> Group:
        return Group(pid, "boot", "42")

    def stop(owned: runfiles.RunFiles, pid: int, grace: float) -> None:
        assert owned is files
        assert (pid, grace) == (23, 0.25)
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
        assert str(caught.value) == (
            "safe error"
            if outcome == "invalid"
            else "run preparation failed; check the clone and credential references"
        )
    assert events == [
        "started",
        "connection-closed",
        "connection-closed",
        *([] if reaped else ["stopped"]),
        ("joined", 0.25),
        "worker-closed",
    ]
    assert files.path("pgid").exists() is (outcome != "ready-timeout")


@pytest.mark.parametrize("ended", [False, True])
def test_owned_and_ended_recorded_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ended: bool
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    files.write("pgid", json.dumps({"pgid": 23, "boot": "boot", "start": "42"}))
    if ended:
        files.write("rc", "0")

    def identity(pid: int) -> Group:
        return Group(pid, "boot", "42")

    stopped: list[tuple[int, float]] = []
    read = files.path
    reads: list[str] = []

    def path(suffix: str) -> Path:
        reads.append(suffix)
        return read(suffix)

    def stop(owned: runfiles.RunFiles, pid: int, grace: float) -> None:
        assert owned is files
        stopped.append((pid, grace))

    monkeypatch.setattr(groups, "identity", identity)
    monkeypatch.setattr(groups, "stop_owned", stop)
    monkeypatch.setattr(files, "path", path)
    assert groups.stop_recorded(files, 0.25) is (Action.DONE if ended else Action.STOP)
    assert stopped == ([] if ended else [(23, 0.25)])
    assert reads[0] == "pgid"


def test_stop_absent_group_and_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def signal_group(pgid: int, number: int) -> None:
        assert pgid == 23
        calls.append(number)
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", signal_group)
    assert groups.alive(23) is False
    groups.stop_started(23, 0)
    assert calls == [0, signal.SIGTERM, 0, 0]


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
    groups.stop_started(23, 0.5)
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


def test_launch_foreground_failure_stops_created_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Child:
        pid = 23

        def wait(self) -> None:
            events.append("wait")

    def foreground(_fd: int, _pid: int) -> None:
        raise OSError

    monkeypatch.setattr(subprocess, "Popen", constant(Child()))
    monkeypatch.setattr(groups, "foreground", foreground)

    def stop(pid: int, grace: float) -> None:
        events.append((pid, grace))

    monkeypatch.setattr(groups, "stop_started", stop)
    with pytest.raises(OSError, match=r"^$"):
        groups.launch(
            ("example",), env={}, cwd="/example", streams=(None, None, None), terminal=0
        )
    assert events == [(23, 0), "wait"]


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
@pytest.mark.parametrize("operation", ["alive", "signal"])
def test_permission_denial_never_means_stopped(
    monkeypatch: pytest.MonkeyPatch, platform: str, operation: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    def denied(_pgid: int, _number: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(groups, "_mac_alive", constant(value=True))
    if operation == "alive" and platform == "darwin":
        assert groups.alive(23) is True
    elif operation == "alive":
        with pytest.raises(PermissionError):
            groups.alive(23)
    elif platform == "darwin":
        with pytest.raises(Invalid, match=r"^run group could not be proven stopped$"):
            groups.stop_started(23, 0)
    else:
        with pytest.raises(PermissionError):
            groups.stop_started(23, 0)


def test_mac_signal_skips_only_proven_dead_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[tuple[int, int]] = []

    def denied(pgid: int, number: int) -> None:
        calls.append((pgid, number))
        raise PermissionError

    monkeypatch.setattr(os, "killpg", denied)
    checked: list[int] = []

    def dead(pgid: int) -> bool:
        checked.append(pgid)
        return False

    monkeypatch.setattr(groups, "_mac_alive", dead)
    assert groups.alive(23) is False
    groups.stop_started(23, 0)
    assert calls == [(23, 0), (23, signal.SIGTERM), (23, 0), (23, 0)]
    assert checked == [23, 23, 23, 23]


class MacGroupLibrary:
    lists = 0
    inspected: list[int]

    def __init__(self, case: str) -> None:
        self.case = case
        self.inspected = []

    def proc_listpids(
        self,
        flavor: int,
        pgid: int,
        buffer: ctypes.Array[ctypes.c_char] | None,
        length: int,
    ) -> int:
        assert (flavor, pgid) == (2, 23)
        if buffer is None:
            assert length == 0
            return {"sizing": 0, "minimal": 4}.get(self.case, 8)
        self.lists += 1
        assert length == (1028 if self.case == "minimal" else 1032)
        assert ctypes.get_errno() == 0
        if self.case == "error":
            ctypes.set_errno(1)
            return 0
        if self.case == "confirm-error" and self.lists == 2:
            ctypes.set_errno(1)
        sizes = {"negative": -1, "full": length, "partial": 3, "empty": 0}
        if self.case in sizes:
            return sizes[self.case]
        struct.pack_into(
            "=ii",
            buffer,
            0,
            23,
            25 if self.case == "changed" and self.lists == 2 else 24,
        )
        return 4 if self.case == "changed-size" and self.lists == 2 else 8

    def proc_pidinfo(
        self,
        pid: int,
        flavor: int,
        arg: int,
        buffer: ctypes.Array[ctypes.c_char],
        length: int,
    ) -> int:
        assert pid in (23, 24)
        assert (flavor, arg, length) == (3, 1, 136)
        self.inspected.append(pid)
        status = 2 if self.case == "live" or (self.case == "mixed" and pid == 24) else 5
        struct.pack_into("=I", buffer, 4, status)
        buffer[8] = b"\xff"
        return 0 if self.case == "member" else 136


@pytest.mark.parametrize(
    "case",
    [
        "zombies",
        "minimal",
        "empty",
        "live",
        "mixed",
        "sizing",
        "error",
        "negative",
        "full",
        "partial",
        "member",
        "changed",
        "changed-size",
        "confirm-error",
    ],
)
def test_mac_group_inspection_fails_closed(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    library = MacGroupLibrary(case)

    def load(path: str, *, use_errno: bool) -> MacGroupLibrary:
        assert path == "/usr/lib/libproc.dylib"
        assert use_errno is True
        return library

    monkeypatch.setattr(ctypes, "CDLL", load)
    monkeypatch.setattr(sys, "platform", "darwin")

    def denied(_pgid: int, _number: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "killpg", denied)
    if case in ("zombies", "minimal", "empty", "live", "mixed"):
        assert groups.alive(23) is (case in ("live", "mixed"))
        assert library.inspected == (
            [] if case == "empty" else [23] if case == "live" else [23, 24]
        )
    else:
        messages = {
            "sizing": "cannot enumerate run group",
            "member": "cannot inspect run group member",
            "changed": "run group changed during inspection",
            "changed-size": "run group changed during inspection",
            "confirm-error": "run group changed during inspection",
        }
        message = messages.get(case, "incomplete run group enumeration")
        with pytest.raises(PermissionError, match=f"^{message}$"):
            groups.alive(23)


def test_sigkill_must_be_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((10, 11, 12, 12.5, 13))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(groups, "alive", constant(value=True))
    monkeypatch.setattr(groups, "_mac_alive", constant(value=True))
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
    with pytest.raises(Invalid, match=r"^run group could not be proven stopped$"):
        groups.stop_started(23, 0)
    assert signals == [(23, signal.SIGTERM), (23, signal.SIGKILL)]
    assert sleeps == [0.02]


@pytest.mark.parametrize("error", [PermissionError, Invalid])
def test_unproven_stop_records_unknown_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    writes: list[tuple[str, str]] = []
    original = files.write

    def write(suffix: str, text: str) -> None:
        writes.append((suffix, text))
        original(suffix, text)

    monkeypatch.setattr(files, "write", write)

    def stop(pgid: int, grace: float) -> None:
        assert (pgid, grace) == (23, 0.25)
        raise error

    monkeypatch.setattr(groups, "stop_started", stop)
    with pytest.raises(error):
        groups.stop_owned(files, 23, 0.25)
    assert files.path("phase").read_text() == "unknown"
    assert files.path("stop-unproven").read_text() == "\n"
    assert not files.path("rc").exists()
    assert writes == [("stop-unproven", "\n"), ("phase", "unknown")]
