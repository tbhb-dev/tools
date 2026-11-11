"""Stage a verified standalone release outside Codex's denied account home."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from tbhbdev.alpha.agent.model import Invalid

DIRECTORY_MODE = 0o755


def inventory(root: Path) -> dict[str, tuple[int, str]]:
    entries: dict[str, tuple[int, str]] = {}
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            target = path.resolve()
            if not target.is_relative_to(root):
                message = f"Codex symlink leaves release: {path}"
                raise Invalid(message)
            value = (stat.S_IFLNK, str(target.relative_to(root)))
        elif stat.S_ISDIR(mode):
            value = (stat.S_IFDIR, "")
        elif stat.S_ISREG(mode):
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, hashlib.sha256).hexdigest()
            value = (stat.S_IMODE(mode), digest)
        else:
            message = f"unsupported Codex release file: {path}"
            raise Invalid(message)
        entries[str(path.relative_to(root))] = value
    entries["."] = (stat.S_IFDIR, "")
    return entries


def copy_release(source: Path, target: Path) -> None:
    target.chmod(DIRECTORY_MODE)
    for path in source.iterdir():
        destination = target / path.name
        if path.is_symlink():
            destination.symlink_to(os.path.relpath(path.resolve(), path.parent))
        elif path.is_dir():
            destination.mkdir()
            copy_release(path, destination)
        else:
            shutil.copy2(path, destination)


def matches(target: Path, expected: dict[str, tuple[int, str]]) -> bool:
    try:
        return (
            not target.is_symlink()
            and inventory(target) == expected
            and all(
                stat.S_IMODE(path.stat().st_mode) == DIRECTORY_MODE
                for path in (target, *target.rglob("*"))
                if path.is_dir() and not path.is_symlink()
            )
        )
    except OSError, Invalid:
        return False


def entrypoint(release: Path) -> Path:
    message = f"invalid Codex entrypoint in {release}"
    try:
        value = json.loads((release / "codex-package.json").read_text())["entrypoint"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Invalid(message) from exc
    if not isinstance(value, str):
        raise Invalid(message)
    path = (release / value).resolve()
    if not path.is_relative_to(release) or not path.is_file():
        raise Invalid(message)
    return path.relative_to(release)


def prepare(executable: str, home: Path) -> tuple[str, tuple[str, ...]]:
    executable = str(Path(executable).resolve())
    release = next(
        (
            parent
            for parent in Path(executable).parents
            if parent.parent.name == "releases"
        ),
        None,
    )
    if release is None or not (release / "codex-package.json").is_file():
        message = f"standalone Codex install required: {executable}"
        raise Invalid(message)
    binary = entrypoint(release)
    expected = inventory(release)
    configured = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    data = configured if configured.is_absolute() else home / ".local/share"
    target = (data / "tbhbdev/codex").resolve() / release.name
    if not matches(target, expected):
        if target.exists() or target.is_symlink():
            target.replace(target.with_name(f"{target.name}.old-{time.time_ns()}"))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(dir=target.parent))
        copy_release(release, temporary)
        if not matches(temporary, expected):
            message = f"Codex copy failed verification: {target}"
            raise Invalid(message)
        temporary.replace(target)
    return str(target / binary), (str(target),)
