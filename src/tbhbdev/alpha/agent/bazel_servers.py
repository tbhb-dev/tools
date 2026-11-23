"""Find live Bazel servers from the host process table and Bazel metadata."""

from __future__ import annotations

import os
import re
import stat
import subprocess  # nosec B404
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.model import Invalid

_OUTPUT_BASE = re.compile(r" --output_base=(/.*?) --workspace_directory=")


@dataclass(frozen=True)
class Process:
    parent: int
    group: int
    command: str


@dataclass(frozen=True)
class Server:
    pid: int
    workspace: Path
    belongs: bool


def listing(uid: int) -> str:
    """Read numeric process IDs and argv on macOS and Linux."""
    try:
        return subprocess.check_output(  # nosec B603
            [
                host_tools.resolve("ps"),
                "-ww",
                "-u",
                str(uid),
                "-o",
                "uid=,pid=,ppid=,pgid=,args=",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            timeout=5,
        )
    except Invalid, OSError, subprocess.SubprocessError:
        return ""


def _processes(uid: int) -> dict[int, Process]:
    processes: dict[int, Process] = {}
    for line in listing(uid).splitlines():
        try:
            owner, pid, parent, group, command = line.split(maxsplit=4)
            if int(owner) == uid:
                processes[int(pid)] = Process(int(parent), int(group), command)
        except ValueError:
            continue
    return processes


def _run_pids(processes: dict[int, Process], group: int) -> set[int]:
    children: defaultdict[int, list[int]] = defaultdict(list)
    pending = [os.getpid()]
    for pid, process in processes.items():
        children[process.parent].append(pid)
        if process.group == group:
            pending.append(pid)
    owned = set(pending)
    while pending:
        for pid in children[pending.pop()]:
            if pid not in owned:
                owned.add(pid)
                pending.append(pid)
    return owned


def matches_pid(base: Path, pid: int) -> bool:
    try:
        status = base.lstat()
        return (
            stat.S_ISDIR(status.st_mode)
            and status.st_uid == os.getuid()
            and (base / "server/server.pid.txt").read_text().strip() == str(pid)
        )
    except OSError, ValueError:
        return False


def _server_workspace(base: Path, pid: int) -> Path | None:
    if not matches_pid(base, pid):
        return None
    try:
        # Bazel writes NUL-separated argv here, preserving spaces in paths.
        args = os.fsdecode((base / "server/cmdline").read_bytes()).split("\0")
    except OSError:
        return None
    for arg in args:
        if arg.startswith("--workspace_directory=/"):
            workspace = Path(arg.removeprefix("--workspace_directory="))
            return workspace if ".." not in workspace.parts else None
    return None


def find(worktree: Path, group: int) -> dict[Path, Server]:
    processes = _processes(os.getuid())
    owned = _run_pids(processes, group)
    servers: dict[Path, Server] = {}
    for pid, process in processes.items():
        if not process.command.startswith("bazel("):
            continue
        match = _OUTPUT_BASE.search(process.command)
        if match is None:
            continue
        base = Path(match[1])
        if ".." in base.parts:
            continue
        workspace = _server_workspace(base, pid)
        if workspace is not None:
            servers[Path(os.path.realpath(base))] = Server(
                pid, workspace, pid in owned or workspace.is_relative_to(worktree)
            )
    return servers
