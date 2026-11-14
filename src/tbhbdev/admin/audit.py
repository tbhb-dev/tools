"""Durable append-only audit records, required before any secret or HTTP call."""

import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4
lazy from pathlib import Path

lazy from tbhbdev.admin.policy import Decision

FILE_MODE = 0o600
DIRECTORY_MODE = 0o700
LOG_NAME = "calls.jsonl"
GENESIS = "0" * 64
TAIL_BLOCK = 4096


class RefusedError(Exception):
    """Only a stable, public reason code belongs in this exception."""


@dataclass(frozen=True)
class Audit:
    directory: Path

    def append(self, record: dict[str, object]) -> None:
        """Append and sync without following a directory or file symlink."""
        try:
            self.directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            # Every call syncs the whole chain, so a sync that failed after the
            # mkdir is retried instead of skipped because the paths now exist.
            for path in (self.directory, *self.directory.parents):
                if not path.is_symlink():
                    self.sync_directory(path)
            directory = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                self.validate(os.fstat(directory), mode=DIRECTORY_MODE)
                self.write(directory, record)
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            msg = "audit_unavailable"
            raise RefusedError(msg) from None

    @staticmethod
    def sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def validate(info: os.stat_result, *, mode: int) -> None:
        kind = stat.S_ISDIR if mode == DIRECTORY_MODE else stat.S_ISREG
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode
            or not kind(info.st_mode)
        ):
            msg = "audit_unavailable"
            raise RefusedError(msg)
        if mode == FILE_MODE and info.st_nlink != 1:
            msg = "audit_unavailable"
            raise RefusedError(msg)

    @staticmethod
    def write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if not written:
                msg = "audit_unavailable"
                raise RefusedError(msg)
            view = view[written:]

    @staticmethod
    def heal_tail(descriptor: int) -> None:
        """End a line that an earlier failed rollback left unfinished."""
        size = os.fstat(descriptor).st_size
        # Reading one byte or two at size - 1 returns the same byte.
        if size and os.pread(descriptor, 1, size - 1) != b"\n":  # pragma: no mutate
            Audit.write_all(descriptor, b"\n")

    @staticmethod
    def chain_head(descriptor: int) -> str:
        """The SHA-256 of the last line, or ``GENESIS`` for an empty log.

        The log ends with one newline, because ``heal_tail`` ran first. The read
        goes backward in blocks from just before that newline.
        """
        size = os.fstat(descriptor).st_size
        if not size:
            return GENESIS
        data = b""
        position = size - 1
        while position and b"\n" not in data:
            start = max(0, position - TAIL_BLOCK)
            block = os.pread(descriptor, position - start, start)
            if len(block) != position - start:
                msg = "audit_unavailable"
                raise RefusedError(msg)
            data = block + data
            position = start
        return hashlib.sha256(data.rpartition(b"\n")[2]).hexdigest()

    @staticmethod
    def same_file(directory: int, descriptor: int) -> None:
        """Refuse a log that was replaced between the open and the lock."""
        held = os.fstat(descriptor)
        current = os.lstat(LOG_NAME, dir_fd=directory)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            code = "audit_unavailable"
            raise RefusedError(code)

    @staticmethod
    def roll_back(descriptor: int, start: int) -> None:
        # A partial line would glue to the next record, so cut it off.
        os.ftruncate(descriptor, start)
        os.fsync(descriptor)

    @staticmethod
    def write(directory: int, record: dict[str, object]) -> None:
        descriptor = os.open(
            LOG_NAME,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            Audit.validate(os.fstat(descriptor), mode=FILE_MODE)
            # The lock covers the size snapshot through the rollback, so a failed
            # writer can't cut off another writer's durable record.
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            Audit.same_file(directory, descriptor)
            Audit.heal_tail(descriptor)
            chained = {**record, "prev": Audit.chain_head(descriptor)}
            data = (json.dumps(chained) + "\n").encode()
            start = os.fstat(descriptor).st_size
            try:
                Audit.write_all(descriptor, data)
                os.fsync(descriptor)
            except OSError, RefusedError:
                Audit.roll_back(descriptor, start)
                raise
            # A rotation after the first check leaves the record in a file that
            # is no longer the log, so the call is refused rather than left unrecorded.
            Audit.same_file(directory, descriptor)
        finally:
            os.close(descriptor)


def record(
    method: str, decision: Decision, reason: str, body: bytes, path: str
) -> dict[str, object]:
    """``path`` is the path as sent, because the policy path is lowercased."""
    return {
        "id": uuid4().hex,
        "time": datetime.now(UTC).isoformat(),
        "uid": os.getuid(),
        "reason": reason,
        "method": method,
        "path": path,
        "decision": "allow" if decision.allowed else "refuse",
        "code": decision.code,
        "status": None,
        "phase": "before",
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
