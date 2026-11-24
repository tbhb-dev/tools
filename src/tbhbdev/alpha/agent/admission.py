"""Reserve a host-wide launch slot for the supervisor's lifetime."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions.hosts import slot_refusal
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import Host

ROOT = Path("/var/tmp/tbhbdev-agent-admission")  # noqa: S108  # nosec B108 -- shared host-wide directory by design; an owner check is a follow-up
_DIRECTORY_MODE = 0o1777


@contextmanager
def _directory(root: Path) -> Generator[int]:
    with suppress(FileExistsError):
        root.mkdir(mode=_DIRECTORY_MODE)
    fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        # mkdir applies the umask, so another launch can see the new directory
        # before its creator widens it, or after the creator died. Widen a
        # directory this account owns instead of refusing.
        if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE and info.st_uid == os.getuid():
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


def counts(root: Path) -> tuple[int, int]:
    """Read active slots without creating admission files."""
    with ExitStack() as stack:
        try:
            fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
            stack.callback(os.close, fd)
            guard = stack.enter_context(_slot(fd, "admission.lock"))
        except FileNotFoundError:
            return 0, 0
        fcntl.flock(guard, fcntl.LOCK_EX)
        return len(_active(fd, Profile.BUILD)), len(_active(fd, Profile.REVIEW))


@contextmanager
def reserve(
    root: Path, host: Host, profile: Profile, limit: int | None
) -> Generator[None]:
    """Check both slot counts and claim capacity under one shared lock.

    LIMIT is the launcher's ``max_concurrent_runs`` for the host. The run
    host's own account config can lack the host, so its default must never
    decide the cap.

    Raises:
        Invalid: LIMIT is None, or no slot is free.
    """
    if limit is None:
        message = f"{host.name}: the request carries no max_concurrent_runs"
        raise Invalid(message)
    with ExitStack() as slot, _directory(root) as directory:
        with _slot(directory, "admission.lock", create=True) as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            builds = _active(directory, Profile.BUILD)
            reviews = _active(directory, Profile.REVIEW)
            reason = slot_refusal(profile, limit, len(builds), len(reviews))
            if reason is not None:
                message = f"{host.name}: {reason}"
                raise Invalid(message)
            active = builds if profile is Profile.BUILD else reviews
            names = (f"{profile}-{i}" for i in range(len(active) + 1))
            name = next(candidate for candidate in names if candidate not in active)
            fd = slot.enter_context(_slot(directory, name, create=True))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
