"""Reclaim old temporary files owned by the run account."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import struct
import sys
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.process import PROC

if TYPE_CHECKING:
    from collections.abc import Callable

_PREFIXES = ("pytest-of-", "rbz.", "launchgate-bazel.")
_DAY = 24 * 60 * 60
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

type Inode = tuple[int, int]


def _inode(info: os.stat_result) -> Inode:
    return info.st_dev, info.st_ino


def _linux_open_files(uid: int) -> set[Inode]:
    opened: set[Inode] = set()
    for process in PROC.iterdir():
        if not process.name.isdecimal():
            continue
        with suppress(FileNotFoundError, ProcessLookupError):
            # proc directory ownership can change for non-dumpable processes.
            uids = next(
                line.split()[1:]
                for line in (process / "status")
                .read_text(errors="replace")
                .splitlines()
                if line.startswith("Uid:")
            )
            if str(uid) not in uids:
                continue
            for ref in (process / "cwd", *(process / "fd").iterdir()):
                with suppress(FileNotFoundError, ProcessLookupError):
                    # These kernel links resolve to the held inode, even after a rename.
                    opened.add(_inode(ref.stat()))
    return opened


# Layouts and flavors from Darwin's sys/proc_info.h.
_PID = struct.Struct("=i")
_FD = struct.Struct("=iI")
_VNODE_ID = struct.Struct("=I4xQ")
_PROC_UID_ONLY = 4
_PROC_PIDLISTFDS = 1
_PROC_PIDVNODEPATHINFO = 9
_PROC_PIDFDVNODEINFO = 1
_PROX_FDTYPE_VNODE = 1
_VNODE_PATH_SIZE = 1176
_VNODE_FD_SIZE = 176
_FILEINFO_SIZE = 24


def _proc_info(call: Callable[..., int], *args: int, size: int = 0) -> bytes:
    """Read a libproc record, treating only closed descriptors and exits as races."""
    if not size:
        size = _proc_call(call, *args, None, 0)
    if not size:
        return b""
    while True:
        buf = ctypes.create_string_buffer(size)
        used = _proc_call(call, *args, buf, size)
        if used < size:
            return buf.raw[:used]
        # A full listing might have been truncated by new processes or FDs.
        size *= 2


def _proc_call(call: Callable[..., int], *args: object) -> int:
    ctypes.set_errno(0)
    used = call(*args)
    error = ctypes.get_errno()
    if used <= 0 and error not in (0, errno.ESRCH, errno.EBADF):
        raise OSError(error, os.strerror(error))
    return used


def _darwin_open_files(uid: int) -> set[Inode]:
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    # Fixed-size vnode records may fill their buffers. Leave one byte spare.
    opened: set[Inode] = set()
    for (pid,) in _PID.iter_unpack(_proc_info(lib.proc_listpids, _PROC_UID_ONLY, uid)):
        cwd = _proc_info(
            lib.proc_pidinfo,
            pid,
            _PROC_PIDVNODEPATHINFO,
            0,
            size=2 * _VNODE_PATH_SIZE + 1,
        )
        if cwd:
            opened.add(_VNODE_ID.unpack_from(cwd))
        fds = _proc_info(lib.proc_pidinfo, pid, _PROC_PIDLISTFDS, 0)
        for fd, kind in _FD.iter_unpack(fds):
            if kind != _PROX_FDTYPE_VNODE:
                continue
            vnode = _proc_info(
                lib.proc_pidfdinfo,
                pid,
                fd,
                _PROC_PIDFDVNODEINFO,
                size=_VNODE_FD_SIZE + 1,
            )
            if vnode:
                opened.add(_VNODE_ID.unpack_from(vnode, _FILEINFO_SIZE))
    return opened


@dataclass(frozen=True)
class _Sweep:
    uid: int
    cutoff: float
    device: int
    opened: set[Inode]

    def stale(self, info: os.stat_result) -> bool:
        return (
            info.st_uid == self.uid
            and info.st_dev == self.device
            and info.st_mtime < self.cutoff
            and _inode(info) not in self.opened
        )

    def walk(
        self, parent: int, name: str, info: os.stat_result, *, delete: bool
    ) -> bool:
        if not self.stale(info):
            return False
        if stat.S_ISDIR(info.st_mode):
            return self.walk_dir(parent, name, info, delete=delete)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            return False
        if delete:
            os.unlink(name, dir_fd=parent)
        return True

    def walk_dir(
        self, parent: int, name: str, info: os.stat_result, *, delete: bool
    ) -> bool:
        # Pin the directory before traversal and refuse symlinks.
        directory = os.open(name, _DIRECTORY, dir_fd=parent)
        try:
            if _inode(os.fstat(directory)) != _inode(info):
                return False
            for child in os.listdir(directory):  # noqa: PTH208 -- the fd pins the directory
                child_info = os.stat(child, dir_fd=directory, follow_symlinks=False)
                if not self.walk(directory, child, child_info, delete=delete):
                    return False
            if delete:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _inode(current) != _inode(info):
                    return False
                os.rmdir(name, dir_fd=parent)
        finally:
            os.close(directory)
        return True


def _candidate(name: str, info: os.stat_result, check: _Sweep) -> bool:
    # Reject compound paths even if they begin with an eligible prefix.
    # Keep entries accessible to other accounts, whose FDs may be invisible.
    return (
        name.startswith(_PREFIXES)
        and "/" not in name
        and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
        and not info.st_mode & 0o077
        and check.stale(info)
    )


def sweep() -> None:
    """Sweep only the system /tmp, including its standard macOS alias."""
    uid = os.getuid()
    if sys.platform == "darwin":
        components, open_files = ("private", "tmp"), _darwin_open_files
    else:
        components, open_files = ("tmp",), _linux_open_files
    with ExitStack() as stack:
        root = os.open("/", _DIRECTORY)
        stack.callback(os.close, root)
        for part in components:
            root = os.open(part, _DIRECTORY, dir_fd=root)
            stack.callback(os.close, root)
        check = _Sweep(uid, time.time() - _DAY, os.fstat(root).st_dev, set())
        candidates = _candidates(root, check)
        if candidates:
            check.opened.update(open_files(uid))
        for name, expected in candidates:
            with suppress(OSError):
                _prune(root, name, expected, check)


def _prune(root: int, name: str, expected: os.stat_result, check: _Sweep) -> None:
    if not _candidate(name, expected, check):
        return
    # Check the entire tree before changing it, then revalidate during deletion.
    for delete in (False, True):
        current = os.stat(name, dir_fd=root, follow_symlinks=False)
        if _inode(current) != _inode(expected) or not _candidate(name, current, check):
            return
        if not check.walk(root, name, current, delete=delete):
            return


def _candidates(root: int, check: _Sweep) -> list[tuple[str, os.stat_result]]:
    candidates = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.startswith(_PREFIXES):
                with suppress(OSError):
                    info = entry.stat(follow_symlinks=False)
                    if _candidate(entry.name, info, check):
                        candidates.append((entry.name, info))
    return candidates
