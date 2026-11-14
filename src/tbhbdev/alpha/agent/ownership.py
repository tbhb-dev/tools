"""Exclusive host-side claims for run names."""

import os
import stat
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.runfiles import RunFiles


class RunExistsError(Invalid):
    """A refused rerun must preserve its previous owner's records."""


OWNER_MODE = 0o600


def verify(files: RunFiles, expected: str) -> None:
    """Refuse unless the owner file is the driver's own private regular file."""
    try:
        # O_NONBLOCK keeps a FIFO planted at the path from blocking the open.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(files.path("owner"), flags)
    except OSError:
        message = f"run {files.name!r} has no usable owner file"
        raise RunExistsError(message) from None
    try:
        info = os.fstat(descriptor)
        private = (
            stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == OWNER_MODE
            and info.st_uid == os.getuid()
        )
        # Read one byte past the expected text so a longer file never matches.
        text = os.read(descriptor, len(expected) + 2) if private else b""
    finally:
        os.close(descriptor)
    if not expected or not private or text != f"{expected}\n".encode():
        message = f"run {files.name!r} has a different owner"
        raise RunExistsError(message)


def claim(
    files: RunFiles, *, claimed: bool = False, expected: str | None = None
) -> None:
    if expected is not None:
        verify(files, expected)
        return
    if claimed:
        return
    try:
        descriptor = os.open(
            files.path("owner"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        message = f"run {files.name!r} already has an owner"
        raise RunExistsError(message) from None
    with os.fdopen(descriptor, "w") as output:
        output.write("host\n")
        output.flush()
        os.fsync(output.fileno())
