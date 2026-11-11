"""Resolve host commands independently of the caller's search path."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess  # nosec B404
import sys
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from os import lstat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

from tbhbdev.alpha.agent.model import Invalid, Os, Settings

_LINUX_LINK_MODE = 0o777

_SEARCH: ContextVar[str] = ContextVar(
    "host_search", default="/usr/bin:/bin:/usr/sbin:/sbin"
)


def search_path(settings: Settings, os: Os) -> tuple[str, ...]:
    entries = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    if os is Os.MACOS:
        entries.extend(("/opt/homebrew/bin", "/usr/local/bin"))
    return (*entries, *settings.sandbox.host_path)


def search() -> str:
    return _SEARCH.get()


@contextmanager
def configured(settings: Settings, os: Os) -> Generator[None]:
    token = _SEARCH.set(":".join(search_path(settings, os)))
    try:
        yield
    finally:
        _SEARCH.reset(token)


def resolve(name: str, path: str | None = None, *, follow_symlinks: bool = True) -> str:
    found = shutil.which(name, path=search() if path is None else path)
    if found is None:
        message = f"host executable {name} was not found"
        raise Invalid(message)
    aliases = (Path(found), *(Path(p) for p in executable_links(Path(found))))
    if any(
        parent.name == "shims" and parent.parent.name == "mise"
        for alias in aliases
        for parent in (alias, alias.parent)
    ):
        message = (
            f"host executable {name} is a mise shim; "
            "configure its fixed install directory"
        )
        raise Invalid(message)
    return str(Path(found).resolve() if follow_symlinks else Path(found).absolute())


def mise_installations(directories: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(alias.parent / "installs")
            for directory in directories
            for alias in (Path(directory), Path(directory).resolve())
            if alias.name == "shims" and alias.parent.name == "mise"
        )
    )


def executable_links(path: Path) -> tuple[str, ...]:
    """Record directory and file links while walking an executable path."""
    return tuple(str(p) for p in executable_components(path) if p.is_symlink())


def executable_components(path: Path) -> Generator[Path]:
    """Walk every directory and link, including targets containing parent steps."""
    path.resolve(strict=True)
    pending = deque(path.parts)
    current = Path(pending.popleft())
    yield current
    while pending:
        part = pending.popleft()
        if part == "..":
            current = current.parent
            continue
        current /= part
        yield current
        if current.is_symlink():
            target = current.readlink()
            if target.is_absolute():
                current = Path(target.anchor)
                yield current
                pending.extendleft(reversed(target.parts[1:]))
            else:
                current = current.parent
                pending.extendleft(reversed(target.parts))


def writable_component(mode: int) -> bool:
    # Linux ordinary symlink modes are fixed at 0777 and grant no write access.
    # Their ownership and the permissions of every parent still need checking.
    ordinary_linux_link = (
        sys.platform == "linux"
        and stat.S_ISLNK(mode)
        and stat.S_IMODE(mode) == _LINUX_LINK_MODE
    )
    return bool(mode & 0o022) and not ordinary_linux_link


def executable_chain(
    executable: str, uid: int, write_roots: tuple[str, ...]
) -> tuple[str, ...]:
    """Trust each traversed component before opening the resolved executable."""
    path = Path(executable)
    roots = tuple(Path(root).resolve() for root in write_roots)
    for component in executable_components(path):
        info = lstat(component)
        if (
            info.st_uid not in (0, uid)
            or writable_component(info.st_mode)
            or any(component.resolve().is_relative_to(root) for root in roots)
        ):
            message = f"untrusted host executable component: {component}"
            raise Invalid(message)
    return tuple(
        dict.fromkeys((executable, *executable_links(path), str(path.resolve())))
    )


def executable_header(current: str) -> bytes:
    fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as incoming:
        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
            message = f"host executable {current} is not a regular file"
            raise Invalid(message)
        return incoming.read(4096)


def env_target(words: list[str], executable: str, search: str) -> str:
    command = words[1:]
    if command[:1] == ["-S"]:
        command = command[1:]
    if not command or command[0].startswith("-") or "=" in command[0]:
        message = f"host script {executable} has an unsupported env interpreter"
        raise Invalid(message)
    return resolve(command[0], search, follow_symlinks=False)


def executable_paths(
    executable: str, search: str, *, uid: int, write_roots: tuple[str, ...]
) -> tuple[str, ...]:
    """Include script interpreters, including an env shim's selected interpreter."""
    paths: list[str] = []
    current = executable
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        chain = executable_chain(current, uid, write_roots)
        paths.extend(chain)
        header = executable_header(chain[-1])
        if not header.startswith(b"#!"):
            return tuple(dict.fromkeys(paths))
        words = shlex.split(header.splitlines()[0][2:].decode())
        if not words or not Path(words[0]).is_absolute():
            message = f"host script {current} has no absolute interpreter"
            raise Invalid(message)
        current = resolve(words[0], follow_symlinks=False)
        interpreter = executable_chain(current, uid, write_roots)
        if any(Path(path).name == "env" for path in interpreter):
            paths.extend(interpreter)
            executable_header(interpreter[-1])
            current = env_target(words, executable, search)
    message = f"host script {executable} has an interpreter cycle"
    raise Invalid(message)


def installation() -> tuple[str, ...]:
    return tuple(
        str(p.resolve())
        for p in (
            Path(sys.prefix),
            Path(sys.base_prefix),
            Path(sys.executable).resolve().parent,
            Path(sys.argv[0]).resolve().parent,
            *(Path(p) for p in sys.path),
        )
    )


def codex_proxy_supported(executable: str, tmpdir: Path) -> bool:
    """Use the feature listing; old CLIs without it fall back to their version."""
    env = {"HOME": str(tmpdir), "CODEX_HOME": str(tmpdir), "PATH": search()}
    try:
        listing = subprocess.run(  # nosec B603 -- resolved and protected executable
            [executable, "features", "list"],
            env=env,
            cwd=tmpdir,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if listing.returncode == 0:
            return any(
                line.split()[:1] == ["network_proxy"]
                for line in listing.stdout.splitlines()
            )
        if "unrecognized subcommand 'features'" not in listing.stderr:
            return False
        version = subprocess.check_output(  # nosec B603 -- resolved and protected executable
            [executable, "--version"],
            env=env,
            cwd=tmpdir,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return False
    found = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)\s*", version)
    return found is not None and tuple(map(int, found.groups())) >= (0, 164, 0)
