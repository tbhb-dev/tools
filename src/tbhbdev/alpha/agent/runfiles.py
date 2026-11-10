"""Private run files and the host's append-only index."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import NAME, Invalid

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import RunRequest

INDEX = "index.jsonl"
INDEX_LOCK = "index.lock"


def state_home(home: Path) -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    if state is not None and Path(state).is_absolute():
        return Path(state)
    return home / ".local" / "state"


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def atomic(path: Path, text: str) -> None:
    """Replace PATH with a complete, mode-600 file on the same filesystem."""
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        Path(temporary).replace(path)
    except BaseException:
        failed = Path(tempfile.mkdtemp(prefix=".failed-", dir=path.parent))
        Path(temporary).replace(failed / "incomplete")
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_owned(source: Path, limit: int, label: str) -> str:
    """Read bounded data through a pinned parent without following a link."""
    directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(
            source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as incoming:
        metadata = os.fstat(incoming.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            message = f"{label} is not an owned regular file"
            raise Invalid(message)
        data = incoming.read(limit + 1)
        if len(data) > limit:
            message = f"{label} exceeds {limit // (1024 * 1024)} MiB"
            raise Invalid(message)
    return data.decode()


def copy_final(source: Path, destination: Path) -> None:
    """Copy a bounded, owned regular file as data, after the harness stops."""
    atomic(destination, read_owned(source, 16 * 1024 * 1024, "final answer"))


@contextmanager
def locked(path: Path) -> Generator[None]:
    """Lock a stable sibling file, never a file that gets replaced."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class RunFiles:
    def __init__(self, root: Path, name: str) -> None:
        if not NAME.fullmatch(name):
            message = f"run name {name!r} must match {NAME.pattern}"
            raise Invalid(message)
        self.root = private_dir(root)
        self.name = name
        self.uncertain = False

    def path(self, suffix: str) -> Path:
        return self.root / f"{self.name}.{suffix}"

    def write(self, suffix: str, text: str) -> None:
        if suffix == "log":
            append_text(self.path(suffix), text)
        else:
            atomic(self.path(suffix), text)

    def receive(self, value: RunRequest) -> None:
        fd = os.open(
            self.path("request.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(request.to_dict(value)) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def snapshot(self) -> dict[str, str]:
        return {
            suffix: self.path(suffix).read_text()
            for suffix in ("phase", "launched", "rc")
            if self.path(suffix).exists()
        }


def append(root: Path, line: dict[str, object]) -> None:
    """Open the index only after acquiring its stable lock."""
    with locked(root / INDEX_LOCK):
        append_text(root / INDEX, json.dumps(line) + "\n")


def latest(root: Path) -> dict[str, dict[str, object]]:
    """Reduce the index to its last line per run, without rewriting it."""
    result: dict[str, dict[str, object]] = {}
    with locked(root / INDEX_LOCK):
        path = root / INDEX
        if path.exists():
            for text in path.read_text().splitlines():
                line = json.loads(text)
                result[line["run"]] = line
    return result


def append_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
