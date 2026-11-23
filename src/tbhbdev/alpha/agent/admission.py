"""Reserve a host-wide launch slot for the supervisor's lifetime."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import Host

ROOT = Path("/var/tmp/tbhbdev-agent-admission")  # noqa: S108  # nosec B108 -- shared host-wide directory by design; an owner check is a follow-up
_DIRECTORY_MODE = 0o1777


@contextmanager
def _directory(root: Path) -> Generator[int]:
    try:
        root.mkdir(mode=_DIRECTORY_MODE)
    except FileExistsError:
        created = False
    else:
        created = True
    fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        if created:
            os.fchmod(fd, _DIRECTORY_MODE)
        if stat.S_IMODE(os.fstat(fd).st_mode) != _DIRECTORY_MODE:
            message = f"agent admission directory must have mode {_DIRECTORY_MODE:o}"
            raise Invalid(message)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _slot(directory: int, name: str, *, create: bool = False) -> Generator[int]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    # Open existing files without O_CREAT so Linux's sticky-directory
    # protection permits opening a slot created by another account.
    if create:
        with suppress(FileExistsError):
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=directory)
            try:
                os.fchmod(fd, 0o644)
            finally:
                os.close(fd)
    fd = os.open(name, flags, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            message = "agent admission slot must be a regular file"
            raise Invalid(message)
        yield fd
    finally:
        os.close(fd)


def _active(directory: int, profile: Profile) -> set[str]:
    active: set[str] = set()
    for name in os.listdir(directory):
        if name.startswith(f"{profile}-"):
            with _slot(directory, name) as fd:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active.add(name)
    return active


@contextmanager
def reserve(root: Path, host: Host, profile: Profile) -> Generator[None]:
    """Check both slot counts and claim capacity under one shared lock."""
    with ExitStack() as slot, _directory(root) as directory:
        with _slot(directory, "admission.lock", create=True) as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            builds = _active(directory, Profile.BUILD)
            reviews = _active(directory, Profile.REVIEW)
            limit = host.max_concurrent_runs
            if len(builds) + len(reviews) >= limit:
                message = (
                    f"{host.name}: no agent slot available "
                    f"(max_concurrent_runs={limit})"
                )
                raise Invalid(message)
            if profile is Profile.BUILD and len(builds) >= limit - 1:
                message = (
                    f"{host.name}: remaining agent capacity is reserved for reviews"
                )
                raise Invalid(message)
            active = builds if profile is Profile.BUILD else reviews
            names = (f"{profile}-{i}" for i in range(len(active) + 1))
            name = next(candidate for candidate in names if candidate not in active)
            fd = slot.enter_context(_slot(directory, name, create=True))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
