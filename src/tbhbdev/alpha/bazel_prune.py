"""Delete abandoned or idle Bazel output bases after checking local processes."""

import argparse
import ctypes
import getpass
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict

BASE_NAME = re.compile(r"[0-9a-fA-F]{32}")
DAY = 86400
SERVER_TIMEOUT = 60
# Darwin returns two path records. Each record has 152 bytes of metadata
# followed by a 1024-byte path.
PATH_INFO_BYTES = 2352
CWD_OFFSET = 152
CWD_END = 1176
PROC_UID_ONLY = 4
PROC_PIDVNODEPATHINFO = 9


class Result(TypedDict):
    base: str
    workspace: str
    action: str
    reason: str
    size_bytes: int


class Summary(TypedDict):
    bases: list[Result]
    dry_run: bool
    freed_bytes: int


def default_root() -> Path:
    name = f"_bazel_{getpass.getuser()}"
    if sys.platform == "darwin":
        return Path("/private/var/tmp") / name
    return Path.home() / ".cache/bazel" / name


def safe_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not value.strip() or root in (Path("/"), Path.home().resolve()):
        message = "unsafe output root"
        raise ValueError(message)
    return root


def mac_processes() -> dict[int, Path | None]:
    """Read same-uid process IDs and working directories through libproc."""
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    list_process_ids = library.proc_listpids
    list_process_ids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    list_process_ids.restype = ctypes.c_int
    pid_info = library.proc_pidinfo
    pid_info.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    pid_info.restype = ctypes.c_int
    count = list_process_ids(PROC_UID_ONLY, os.getuid(), None, 0)
    if count < ctypes.sizeof(ctypes.c_int):
        message = "cannot enumerate local processes"
        raise OSError(message)
    # Allow for processes appearing between the size query and the read.
    process_ids = (ctypes.c_int * (count // ctypes.sizeof(ctypes.c_int) + 256))()
    used = list_process_ids(
        PROC_UID_ONLY, os.getuid(), process_ids, ctypes.sizeof(process_ids)
    )
    if used < ctypes.sizeof(ctypes.c_int) or used >= ctypes.sizeof(process_ids):
        message = "incomplete local process list"
        raise OSError(message)
    result: dict[int, Path | None] = {}
    for pid in process_ids[: used // ctypes.sizeof(ctypes.c_int)]:
        if pid <= 0:
            continue
        buffer = ctypes.create_string_buffer(PATH_INFO_BYTES)
        length = pid_info(pid, PROC_PIDVNODEPATHINFO, 0, buffer, PATH_INFO_BYTES)
        cwd = buffer.raw[CWD_OFFSET:CWD_END].partition(b"\0")[0]
        result[pid] = (
            Path(os.fsdecode(cwd)) if length == PATH_INFO_BYTES and cwd else None
        )
    return result


def processes() -> dict[int, Path | None]:
    if sys.platform == "darwin":
        return mac_processes()
    result: dict[int, Path | None] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid == os.getuid():
                result[int(entry.name)] = working_directory(entry / "cwd")
        except FileNotFoundError, ProcessLookupError:
            continue
        except PermissionError:
            result[int(entry.name)] = None
    return result


def working_directory(link: Path) -> Path:
    """Normalize Linux's marker for an unlinked working directory."""
    cwd = link.readlink()
    if cwd.exists():
        return cwd
    return Path(str(cwd).removesuffix(" (deleted)"))


def disk_size(path: Path) -> int:
    """Count allocated bytes without following links or counting hard links twice."""
    pending = [path]
    seen: set[tuple[int, int]] = set()
    size_bytes = 0
    while pending:
        current = pending.pop()
        info = current.lstat()
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        size_bytes += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return size_bytes


def newest_mtime(base: Path) -> float:
    latest = base.stat().st_mtime
    for name in ("server", "action_cache", "command.log"):
        try:
            latest = max(latest, (base / name).lstat().st_mtime)
        except FileNotFoundError:
            continue
    return latest


def server_pid(base: Path) -> int:
    try:
        return int((base / "server/server.pid.txt").read_text().strip())
    except OSError, ValueError:
        return 0


def selection(base: Path, workspace: Path, cutoff: float) -> str:
    active = processes()
    if any(
        cwd is not None and (cwd.is_relative_to(workspace) or cwd.is_relative_to(base))
        for cwd in active.values()
    ):
        return "busy"
    if not workspace.is_dir():
        return "gone"
    if newest_mtime(base) < cutoff:
        return "idle"
    return "server-alive" if server_pid(base) in active else "fresh"


def shutdown(base: Path, workspace: Path) -> bool:
    """Ask Bazel to stop; never signal the server."""
    tool = shutil.which("bazelisk") or shutil.which("bazel")
    if tool is None:
        return False
    with tempfile.TemporaryDirectory() as temporary:
        cwd = workspace if workspace.is_dir() else Path(temporary)
        try:
            subprocess.run(  # nosec B603 B607
                [tool, f"--output_base={base}", "shutdown"],
                cwd=cwd,
                timeout=SERVER_TIMEOUT,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError, subprocess.TimeoutExpired:
            return False
    return True


def delete_base(base: Path, root: Path) -> None:
    if base.is_symlink() or base.resolve().parent != root.resolve():
        message = "unsafe output base"
        raise OSError(message)
    subprocess.run(["chmod", "-R", "u+w", "--", str(base)], check=True)  # nosec B603 B607
    shutil.rmtree(base)


def inspect_base(base: Path, root: Path, cutoff: float, *, dry_run: bool) -> Result:
    result: Result = {
        "base": str(base),
        "workspace": "",
        "action": "keep",
        "reason": "unreadable-workspace",
        "size_bytes": disk_size(base),
    }
    try:
        text = (base / "DO_NOT_BUILD_HERE").read_text().strip()
    except OSError, UnicodeError:
        return result
    result["workspace"] = text
    if not text:
        result["reason"] = "empty-workspace"
        return result
    workspace = Path(text).resolve()
    reason = selection(base, workspace, cutoff)
    result["reason"] = reason
    if reason not in {"gone", "idle"}:
        return result
    if dry_run:
        return {**result, "action": "would-prune"}
    return finish_prune(result, base, root, workspace, cutoff)


def finish_prune(
    result: Result, base: Path, root: Path, workspace: Path, cutoff: float
) -> Result:
    pid = server_pid(base)
    if pid in processes():
        shutdown(base, workspace)
    if pid in processes():
        result["reason"] = "server-alive"
        return result
    # Check again after shutdown, which can take up to a minute.
    if selection(base, workspace, cutoff) == "busy":
        result["reason"] = "busy"
        return result
    result["action"] = "prune"
    try:
        delete_base(base, root)
    except OSError, subprocess.CalledProcessError:
        result["reason"] = "delete-failed"
    else:
        result["action"] = "pruned"
    return result


def run(root: Path, days: int, *, dry_run: bool) -> Summary:
    results: list[Result] = []
    cutoff = time.time() - days * DAY
    if root.exists():
        results.extend(
            inspect_base(base, root, cutoff, dry_run=dry_run)
            for base in sorted(root.iterdir())
            if BASE_NAME.fullmatch(base.name)
            and not base.is_symlink()
            and (base / "DO_NOT_BUILD_HERE").is_file()
        )
    freed_bytes = sum(
        item["size_bytes"]
        for item in results
        if item["action"] in {"pruned", "would-prune"}
    )
    return {"bases": results, "dry_run": dry_run, "freed_bytes": freed_bytes}


def report(summary: Summary, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary))
    else:
        for item in summary["bases"]:
            print(
                f"{item['action']} {item['reason']} {item['size_bytes']} "
                f"{item['workspace']}"
            )
        label = "would free" if summary["dry_run"] else "freed"
        print(f"{label} {summary['freed_bytes']} bytes")
    if not summary["dry_run"]:
        journal = Path.home() / ".local/state/tbhbdev/bazel-prune.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a") as stream:
            stream.write(json.dumps(summary) + "\n")


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-idle-days", type=int, default=14)
    parser.add_argument("--output-root", default=str(default_root()))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.max_idle_days < 0:
        print(f"{prog}: --max-idle-days must be nonnegative", file=sys.stderr)
        return 2
    try:
        root = safe_root(args.output_root)
    except ValueError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    try:
        summary = run(root, args.max_idle_days, dry_run=args.dry_run)
        report(summary, as_json=args.json)
    except OSError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    return int(any(item["reason"] == "delete-failed" for item in summary["bases"]))
