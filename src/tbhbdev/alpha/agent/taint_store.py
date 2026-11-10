"""Append-only coordinator audit records, independent of the run host."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from secrets import token_hex
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import driver
from tbhbdev.alpha.agent.decisions import taint
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Generator


def _check(info: os.stat_result, kind: int, mode: int) -> None:
    if info.st_uid != os.geteuid():
        message = "taint store has wrong ownership"
        raise Invalid(message)
    if info.st_mode != kind | mode:
        message = f"taint store has wrong type or mode (requires {mode:o})"
        raise Invalid(message)


@contextmanager
def _directory(*, create: bool) -> Generator[int]:
    # Parent aliases (including macOS's temp root) are supported; the store
    # directory and records themselves must never be symbolic links.
    root = driver.directory()
    if create:
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ExitStack() as stack:
        descriptor = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, descriptor)
        for part in (root.name, "taint"):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            stack.callback(os.close, descriptor)
            _check(os.fstat(descriptor), stat.S_IFDIR, 0o700)
        yield descriptor


def _incomplete(descriptor: int) -> bool:
    length = os.fstat(descriptor).st_size
    return length > 0 and os.pread(descriptor, length, length - 1) != b"\n"


@contextmanager
def _file(
    host: str, *, write: bool = False, repair: bool = False
) -> Generator[tuple[int, int]]:
    taint.host_identity(host)
    name = f"{host}.jsonl"
    with _directory(create=write) as parent, ExitStack() as stack:
        # Keep the lock on the directory inode across broken-file replacement.
        fcntl.flock(parent, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND if write else os.O_RDONLY
        descriptor = os.open(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
        )
        stack.callback(os.close, descriptor)
        _check(os.fstat(descriptor), stat.S_IFREG, 0o600)
        if repair and _incomplete(descriptor):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            os.rename(
                name,
                f"{name}.broken-{stamp}-{token_hex(8)}",
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            descriptor = os.open(name, flags | os.O_EXCL, 0o600, dir_fd=parent)
            stack.callback(os.close, descriptor)
        yield descriptor, parent


def _append(record: taint.Taint | taint.Clearance, kind: str) -> None:
    data = (json.dumps({"kind": kind, **asdict(record)}) + "\n").encode()
    with _file(record.host, write=True, repair=isinstance(record, taint.Clearance)) as (
        descriptor,
        parent,
    ):
        if os.write(descriptor, data) != len(data):
            message = "incomplete taint store append"
            raise OSError(message)
        os.fsync(descriptor)
        os.fsync(parent)


def record_taint(record: taint.Taint) -> None:
    _append(record, "taint")


def record_clearance(record: taint.Clearance) -> None:
    _append(record, "clearance")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        message = "duplicate taint record field"
        raise Invalid(message)
    return result


def _parse(line: bytes, host: str) -> taint.Taint | taint.Clearance:
    if not line.endswith(b"\n"):
        message = "incomplete taint record line"
        raise Invalid(message)
    data: dict[str, object] = json.loads(line.decode(), object_pairs_hook=_object)
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        message = "taint record must be an object of strings"
        raise Invalid(message)
    fields = {key: str(value) for key, value in data.items()}
    kind = fields.pop("kind")
    record = taint.Taint(**fields) if kind == "taint" else taint.Clearance(**fields)
    if kind not in {"taint", "clearance"} or record.host != host:
        message = "taint record has wrong kind or host"
        raise Invalid(message)
    return record


def load(host: str) -> taint.State:
    """Missing records are unknown; unreadable or invalid records fail closed."""
    taint.host_identity(host)
    records: list[taint.Taint | taint.Clearance] = []
    try:
        with (
            _file(host) as (descriptor, _parent),
            os.fdopen(os.dup(descriptor), "rb") as source,
        ):
            records.extend(_parse(line, host) for line in source)
    except FileNotFoundError:
        return taint.State(host)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return taint.State(host, "tainted", problem=f"invalid local taint store: {exc}")
    return taint.state(records, host)
