"""Exclusive host-side claims for run names."""

import os
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.runfiles import RunFiles


class RunExistsError(Invalid):
    """A refused rerun must preserve its previous owner's records."""


def claim(files: RunFiles, *, claimed: bool = False, owner: str | None = None) -> None:
    if claimed:
        return
    if owner is not None:
        try:
            recorded = runfiles.read_owned(files.path("owner"), 1024, "run owner")
        except (OSError, ValueError) as exc:
            message = f"run {files.name!r} has no matching owner"
            raise RunExistsError(message) from exc
        if recorded != f"{owner}\n":
            message = f"run {files.name!r} has no matching owner"
            raise RunExistsError(message)
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
