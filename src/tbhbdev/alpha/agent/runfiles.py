"""Private run files and the host's append-only index."""

from __future__ import annotations

import fcntl
import json
import os
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


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def atomic(path: Path, text: str) -> None:
    """Replace PATH with a complete, mode-600 file on the same filesystem."""
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    Path(temporary).replace(path)


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

    def path(self, suffix: str) -> Path:
        return self.root / f"{self.name}.{suffix}"

    def write(self, suffix: str, text: str) -> None:
        atomic(self.path(suffix), text)

    def receive(self, value: RunRequest) -> None:
        self.write("request.json", json.dumps(request.to_dict(value)) + "\n")

    def snapshot(self) -> dict[str, str]:
        return {
            suffix: self.path(suffix).read_text()
            for suffix in ("phase", "launched", "rc")
            if self.path(suffix).exists()
        }


def append(root: Path, line: dict[str, object]) -> None:
    """Open the index only after acquiring its stable lock."""
    with locked(root / INDEX_LOCK):
        fd = os.open(root / INDEX, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a") as out:
            out.write(json.dumps(line) + "\n")
            out.flush()
            os.fsync(out.fileno())


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
