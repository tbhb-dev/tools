"""Durable append-only audit records, required before any secret or HTTP call."""

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


class RefusedError(Exception):
    """Only a stable, public reason code belongs in this exception."""


@dataclass(frozen=True)
class Audit:
    directory: Path

    def append(self, record: dict[str, object]) -> None:
        """Append and sync without following a directory or file symlink."""
        try:
            self.directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
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
    def write(directory: int, record: dict[str, object]) -> None:
        descriptor = os.open(
            "calls.jsonl",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            Audit.validate(os.fstat(descriptor), mode=FILE_MODE)
            data = (json.dumps(record) + "\n").encode()
            # One append system call prevents concurrent records from interleaving.
            if os.write(descriptor, data) != len(data):
                msg = "audit_unavailable"
                raise RefusedError(msg)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def record(
    method: str, decision: Decision, reason: str, body: bytes
) -> dict[str, object]:
    return {
        "id": uuid4().hex,
        "time": datetime.now(UTC).isoformat(),
        "uid": os.getuid(),
        "reason": reason,
        "method": method,
        "path": decision.path,
        "decision": "allow" if decision.allowed else "refuse",
        "code": decision.code,
        "status": None,
        "phase": "before",
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
