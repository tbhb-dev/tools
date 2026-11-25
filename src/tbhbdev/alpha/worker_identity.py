"""Whether a non-interactive caller descends from the registered worker.

`[agent.worker] path` in the user config names the worker executable. A caller
with no tmux pane counts as the worker only when an ancestor process runs that
executable. Any lookup failure or loop means no match.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_WORKFLOW = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_INIT = 1
_PS_TIMEOUT_SECONDS = 5
_PIDPATH_MAX = 4096  # the libproc path buffer size


@dataclass(frozen=True)
class Process:
    """One process: its parent's pid and its executable path, "" when unknown."""

    ppid: int
    exe: str


type Reader = Callable[[int], Process | None]


def workflow(text: str) -> str:
    """The worker workflow id TEXT, which names a launcher or a closer.

    Raises:
        argparse.ArgumentTypeError: TEXT isn't 1 to 128 of `A-Za-z0-9._:-`.
    """
    if not _WORKFLOW.fullmatch(text):
        message = "must be 1 to 128 characters of A-Za-z0-9._:-"
        raise argparse.ArgumentTypeError(message)
    return text


def name(workflow_id: str) -> str:
    """The launcher or closer name for a worker workflow."""
    return f"worker/{workflow_id}"


def why_not(registered: str | None, pid: int, read: Reader) -> str | None:
    """Why PID isn't the registered worker's descendant, or None when it is.

    The walk starts at PID's parent and ends after pid 1. READ answers for one
    pid, or None when it can't.
    """
    if registered is None:
        return "no [agent.worker] path is registered in the config"
    target = os.path.realpath(registered)
    why = f"no ancestor process runs the registered worker {target}"
    seen = {pid}
    current = read(pid)
    while current is not None and current.ppid not in seen:
        seen.add(current.ppid)
        parent = read(current.ppid)
        if parent is not None and parent.exe and os.path.realpath(parent.exe) == target:
            return None
        if current.ppid == _INIT:
            break
        current = parent
    return why


def linux_reader(proc: str = "/proc") -> Reader:
    """A reader of PROC's `PID/stat` for the parent and `PID/exe` for the path."""

    def read(pid: int) -> Process | None:
        try:
            stat = Path(proc, str(pid), "stat").read_bytes()
            # The command name sits in parentheses and may hold anything.
            ppid = int(stat.rpartition(b")")[2].split()[1])
            return Process(ppid, Path(proc, str(pid), "exe").readlink().as_posix())
        except OSError, ValueError, IndexError:
            return None

    return read


def _ps(field: str, pid: int) -> str | None:
    try:
        done = subprocess.run(  # nosec B603 -- fixed argv
            ["/bin/ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout.strip()


def _pidpath(pid: int) -> str | None:
    """PID's real executable path from libproc, whatever way it was launched."""
    buffer = ctypes.create_string_buffer(_PIDPATH_MAX)
    try:
        libc = ctypes.CDLL("libSystem.dylib", use_errno=True)
        length = libc.proc_pidpath(pid, buffer, _PIDPATH_MAX)
    except OSError, AttributeError:
        return None
    return os.fsdecode(buffer.raw[:length]) if length > 0 else None


def darwin_reader(
    ps: Callable[[str, int], str | None] = _ps,
    pidpath: Callable[[int], str | None] = _pidpath,
) -> Reader:
    """A reader of `ps` for the parent and libproc `proc_pidpath` for the path.

    `ps -o comm=` shows the name as launched, which can be relative.
    """

    def read(pid: int) -> Process | None:
        parent = ps("ppid", pid)
        if parent is None or not parent.isdecimal():
            return None
        return Process(int(parent), pidpath(pid) or "")

    return read


def reader() -> Reader:
    """The reader for this platform."""
    return linux_reader() if sys.platform == "linux" else darwin_reader()


def refusal(registered: str | None) -> str | None:
    """Why the current process isn't the registered worker's descendant, or None."""
    return why_not(registered, os.getpid(), reader())
