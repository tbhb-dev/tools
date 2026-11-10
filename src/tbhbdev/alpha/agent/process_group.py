"""Launch and stop only groups created by this supervisor."""

from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import signal
import struct
import subprocess  # nosec B404
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.decisions.run_state import Group
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from types import FrameType
    from typing import IO

    from tbhbdev.alpha.agent.runfiles import RunFiles

# proc_bsdinfo in Apple's sys/proc_info.h: two uint64 start fields at byte 120.
_BSD_SIZE = 136
_BSD_START = 120
_PROC_PIDTBSDINFO = 3
_ZOMBIE = 5


def boot_id() -> str:
    if sys.platform == "linux":
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return subprocess.check_output(  # nosec B603 B607
        [host_tools.resolve("sysctl"), "-n", "kern.boottime"], text=True, timeout=5
    ).strip()


def _mac_start(pid: int) -> str | None:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(_BSD_SIZE)
    size = library.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buffer, _BSD_SIZE)
    if size != _BSD_SIZE or int.from_bytes(buffer[4:8], sys.byteorder) == _ZOMBIE:
        return None
    seconds, micros = struct.unpack_from("=QQ", buffer, _BSD_START)
    return f"{seconds}:{micros}"


def leader_start(pid: int) -> str | None:
    if sys.platform != "linux":
        return _mac_start(pid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    # The command may contain spaces and parentheses. Field 3 follows its last ).
    else:
        return None if fields[0] == "Z" else fields[19]


def identity(pgid: int) -> Group | None:
    start = leader_start(pgid)
    return None if start is None else Group(pgid, boot_id(), start)


def record(files: RunFiles, pgid: int) -> None:
    group = identity(pgid)
    if group is None:
        message = "run group leader exited before it could be recorded"
        raise Invalid(message)
    files.write("pgid", json.dumps(asdict(group)) + "\n")


def alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pgid: int, number: signal.Signals) -> bool:
    """Return whether signalling was permitted; ESRCH is already stopped."""
    try:
        os.killpg(pgid, number)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    return True


def _wait_stopped(
    pgid: int, deadline: float, reap: Callable[[], object] | None
) -> bool:
    while True:
        if reap is not None:
            reap()
        if not alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def stop_started(
    pgid: int,
    grace: float,
    reap: Callable[[], object] | None = None,
    report: Callable[[str], None] | None = None,
) -> bool:
    """Reap the leader and prove ESRCH, escalating even after an EPERM."""
    for number, delay in ((signal.SIGTERM, grace), (signal.SIGKILL, 0.2)):
        try:
            if not _signal(pgid, number) and report is not None:
                report(f"permission denied sending {number.name} to run group\n")
            if _wait_stopped(pgid, time.monotonic() + delay, reap):
                return True
        except OSError:
            continue
    return False


def unknown(files: RunFiles) -> None:
    files.uncertain = True
    for suffix, text in (
        ("stop-unproven", "\n"),
        ("phase", "unknown"),
        ("log", "run group stop unproven; phase unknown\n"),
    ):
        with suppress(OSError):
            files.write(suffix, text)


def stop_owned(
    files: RunFiles,
    pgid: int,
    grace: float,
    reap: Callable[[], object] | None = None,
) -> bool:
    try:
        stopped = (
            stop_started(pgid, grace, reap, lambda text: files.write("log", text))
            is not False
        )
    except OSError, Invalid:
        stopped = False
    if not stopped:
        unknown(files)
    return stopped


INTERRUPTS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)


_CLEANUP: list[None] = []


def cleaning() -> bool:
    return bool(_CLEANUP)


@contextmanager
def ignore_signals() -> Generator[None]:
    # Mark cleanup before changing handlers: another thread can receive a signal.
    _CLEANUP.append(None)
    try:
        previous = {s: signal.signal(s, signal.SIG_IGN) for s in INTERRUPTS}
        try:
            yield
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    finally:
        _CLEANUP.pop()


@contextmanager
def defer_signals() -> Generator[None]:
    """Finish acquiring a child before delivering a supervisor interrupt.

    Caught handlers reset on exec, so the child inherits no blocked signals.
    """
    received: list[tuple[int, FrameType | None]] = []

    def receive(number: int, frame: FrameType | None) -> None:
        received.append((number, frame))

    previous = {s: signal.signal(s, receive) for s in INTERRUPTS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if received:
            number, frame = received[0]
            handler = previous[signal.Signals(number)]
            if callable(handler):
                handler(number, frame)


@dataclass(frozen=True)
class Launch:
    command: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    streams: tuple[IO[str] | int | None, IO[str] | int | None, IO[str] | int | None]
    terminal: int | None


def launch(
    command: tuple[str, ...],
    *,
    env: dict[str, str],
    cwd: str,
    streams: tuple[IO[str] | int | None, IO[str] | int | None, IO[str] | int | None],
    terminal: int | None,
) -> subprocess.Popen[str]:
    """Pass the exact policy environment and give the harness its own group."""
    return launch_owned(Launch(command, env, cwd, streams, terminal))


def launch_owned(
    spec: Launch, *, started: Callable[[subprocess.Popen[str]], None] | None = None
) -> subprocess.Popen[str]:
    """Transfer cleanup ownership before terminal setup can fail."""
    stdin, stdout, stderr = spec.streams
    child = None
    try:
        with defer_signals():
            child = subprocess.Popen(  # nosec B603 -- argv builder fixes the harness name
                spec.command,
                env=spec.env,
                cwd=spec.cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=spec.terminal is None,
                process_group=0 if spec.terminal is not None else None,
            )
            if started is not None:
                started(child)
            if spec.terminal is not None:
                foreground(spec.terminal, child.pid)
                _signal(child.pid, signal.SIGCONT)
    except BaseException:
        if child is not None and started is None:
            with ignore_signals():
                if stop_started(child.pid, 0, child.poll) is not False:
                    child.wait()
        raise
    else:
        return child


def foreground(terminal: int, pgid: int) -> None:
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(terminal, pgid)
    finally:
        signal.signal(signal.SIGTTOU, previous)


def prepare_child(connection: Connection, operation: Callable[[], object]) -> None:
    os.setpgid(0, 0)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    connection.send(None)
    connection.recv()
    try:
        connection.send((True, operation()))
    except Invalid as exc:
        connection.send((False, str(exc)))
    except OSError, ValueError, subprocess.SubprocessError:
        # Third-party errors can contain credentials. Send no exception text.
        connection.send((False, None))
    finally:
        connection.close()


def worker_pid(worker: BaseProcess) -> int:
    pid = worker.pid
    if pid is None:
        message = "setup worker has not started"
        raise Invalid(message)
    return pid


def prepare(
    operation: Callable[[], object], files: RunFiles, deadline: float, grace: float
) -> object:
    """Bound all setup, including blocking library reads, by one deadline.

    A setup worker leads the recorded group so stopping a hung library read
    leaves the supervisor alive to write rc. A pipe carries results in memory.
    """
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    worker = context.Process(target=prepare_child, args=(child, operation))
    worker.start()
    pid = worker_pid(worker)
    child.close()
    ready = False
    try:
        if not parent.poll(max(0, deadline - time.monotonic())):
            raise TimeoutError
        parent.recv()
        ready = True
        record(files, pid)
        parent.send(None)
        if not parent.poll(max(0, deadline - time.monotonic())):
            raise TimeoutError
        ok, result = parent.recv()
        if not ok:
            message = (
                result
                or "run preparation failed; check the clone and credential references"
            )
            raise Invalid(message)
        return result
    finally:
        with ignore_signals():
            parent.close()
            if ready and worker.exitcode is None:
                stop_owned(files, pid, grace, worker.is_alive)
            worker.join(timeout=grace)
            try:
                if worker.is_alive():
                    worker.kill()
                    worker.join(timeout=grace)
                worker.close()
            except OSError, ValueError:
                unknown(files)
