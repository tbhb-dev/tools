# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Parent PID and command line of a process, without running ``ps``.

A Codex sandbox denies ``ps`` (it needs ``sysctl``), so the identity hooks read
the same facts directly: ``/proc`` on Linux, libproc through ctypes on macOS.
Each lookup returns None when the process is gone or the read is denied; the
caller then falls back to ``ps``.
"""

import ctypes
import struct
import sys
from pathlib import Path

PROC = Path("/proc")

# <sys/proc_info.h>: proc_pidinfo flavor and the size of struct proc_bsdinfo,
# whose pbi_ppid is the fifth uint32.
PROC_PIDTBSDINFO = 3
BSDINFO_SIZE = 136
PPID_OFFSET = 16
# <sys/sysctl.h>: CTL_KERN, KERN_PROCARGS2.
PROCARGS_MIB = (1, 49)
# <sys/syslimits.h> PATH_MAX times four, proc_pidpath's documented buffer size.
PIDPATH_SIZE = 4096
ARGC_BYTES = 4


def linux_row(pid: str, proc: Path = PROC) -> tuple[str, str] | None:
    """(parent PID, command line) from /proc, with argv joined by spaces."""
    try:
        stat = (proc / pid / "stat").read_text(errors="replace")
        cmdline = (proc / pid / "cmdline").read_bytes()
    except OSError:
        return None
    # The command name sits in parentheses and may itself hold spaces or ")".
    head, _, tail = stat.rpartition(")")
    fields = tail.split()
    if len(fields) < 2:  # noqa: PLR2004 -- state and ppid
        return None
    args = cmdline.replace(b"\0", b" ").decode(errors="replace").strip()
    # A kernel thread has no argv; ps shows its name in brackets.
    return fields[1], args or f"[{head.partition('(')[2]}]"


def darwin_args(libc: ctypes.CDLL, pid: int) -> str | None:
    """The argv of a process by KERN_PROCARGS2, or None when sysctl is denied."""
    mib = (ctypes.c_int * 3)(*PROCARGS_MIB, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.pointer(size), None, 0) != 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.pointer(size), None, 0) != 0:
        return None
    raw = buf.raw[: size.value]
    argc = int.from_bytes(raw[:ARGC_BYTES], sys.byteorder)
    # argc, the executable path, NUL padding, then argv and the environment.
    _, _, rest = raw[ARGC_BYTES:].partition(b"\0")
    argv = rest.lstrip(b"\0").split(b"\0")[:argc]
    # Like ps, drop the blanks a retitled process pads its argv with.
    return b" ".join(argv).decode(errors="replace").strip() or None


def darwin_path(libc: ctypes.CDLL, pid: int) -> str | None:
    """The executable path of a process by proc_pidpath, or None."""
    buf = ctypes.create_string_buffer(PIDPATH_SIZE)
    length = libc.proc_pidpath(pid, buf, PIDPATH_SIZE)
    return buf.raw[:length].decode(errors="replace") if length > 0 else None


def darwin_row(pid: str, libc: ctypes.CDLL | None = None) -> tuple[str, str] | None:
    """(parent PID, command line) through libproc.

    The parent comes from proc_pidinfo. The command line comes from
    KERN_PROCARGS2, and from the executable path alone where a sandbox denies
    sysctl. The path gives no ``--amend`` flag, and for a versioned install
    its last component isn't ``claude``, so the walk then runs on to PID 1.
    """
    libc = libc or ctypes.CDLL(None)
    number = int(pid)
    info = ctypes.create_string_buffer(BSDINFO_SIZE)
    if libc.proc_pidinfo(number, PROC_PIDTBSDINFO, 0, info, BSDINFO_SIZE) != (
        BSDINFO_SIZE
    ):
        return None
    cmd = darwin_args(libc, number) or darwin_path(libc, number)
    if not cmd:
        return None
    return str(struct.unpack_from("I", info.raw, PPID_OFFSET)[0]), cmd


def process_row(pid: str) -> tuple[str, str] | None:
    """(parent PID, command line) of a process without ps, or None."""
    if not pid.isdecimal():
        return None
    if sys.platform.startswith("linux"):
        return linux_row(pid)
    if sys.platform == "darwin":
        return darwin_row(pid)
    return None
