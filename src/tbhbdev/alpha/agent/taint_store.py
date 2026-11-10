"""Append-only coordinator audit records, independent of the run host."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict
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
    root = driver.directory().absolute()
    with ExitStack() as stack:
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, descriptor)
        parts = (*root.parts[1:], "taint")
        for index, part in enumerate(parts):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            stack.callback(os.close, descriptor)
            if index >= len(parts) - 2:
                _check(os.fstat(descriptor), stat.S_IFDIR, 0o700)
        yield descriptor


def _filename(host: str, account: str) -> str:
    taint.identity(host, account)
    return f"{host}.{account}.jsonl"


@contextmanager
def _file(host: str, account: str, *, write: bool = False) -> Generator[int]:
    name = _filename(host, account)
    with _directory(create=write) as parent:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND if write else os.O_RDONLY
        descriptor = os.open(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
        )
        try:
            _check(os.fstat(descriptor), stat.S_IFREG, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            yield descriptor
        finally:
            os.close(descriptor)


def _append(record: taint.Taint | taint.Clearance, kind: str) -> None:
    data = (json.dumps({"kind": kind, **asdict(record)}) + "\n").encode()
    with _file(record.host, record.account, write=True) as descriptor:
        if os.write(descriptor, data) != len(data):
            message = "incomplete taint store append"
            raise OSError(message)
        os.fsync(descriptor)


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


def _parse(line: bytes, host: str, account: str) -> taint.Taint | taint.Clearance:
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
    if kind not in {"taint", "clearance"} or (record.host, record.account) != (
        host,
        account,
    ):
        message = "taint record has wrong kind, host or account"
        raise Invalid(message)
    return record


def load(host: str, account: str) -> taint.State:
    """Missing records are clean; unreadable or invalid records fail closed."""
    taint.identity(host, account)
    taints: list[taint.Taint] = []
    clearances: list[taint.Clearance] = []
    try:
        with (
            _file(host, account) as descriptor,
            os.fdopen(os.dup(descriptor), "rb") as source,
        ):
            for line in source:
                record = _parse(line, host, account)
                if isinstance(record, taint.Taint):
                    taints.append(record)
                else:
                    clearances.append(record)
    except FileNotFoundError:
        return taint.State(host, account)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return taint.State(host, account, problem=f"invalid local taint store: {exc}")
    return taint.state(taints, clearances, host, account)
