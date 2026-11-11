"""Temporary repositories and literal requests for host-side agent tests."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    import pytest

from agent_example import settings
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent import host_tools, model
from tbhbdev.alpha.agent.decisions import request


def git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args],
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        timeout=10,
    ).strip()


def commit(path: Path, message: str, *, env: dict[str, str] | None = None) -> str:
    git(path, "add", ".", env=env)
    git(
        path,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.com",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-qm",
        message,
        env=env,
    )
    return git(path, "rev-parse", "HEAD", env=env)


def repository(root: Path) -> tuple[Path, Path]:
    origin = root / "origin"
    seed = root / "seed"
    clone = root / "clone"
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "--bare", "--initial-branch=main", str(origin))
    git(root, "init", "--initial-branch=main", str(seed))
    (seed / "tracked").write_text("initial\n")
    commit(seed, "initial")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main")
    git(root, "clone", str(origin), str(clone))
    return seed, clone


def run_request(**changes: object) -> model.RunRequest:
    return request.parse(BUILD | changes, host_allows_unsandboxed=True)


def host_settings() -> model.Settings:
    original = settings()
    references = model.Credentials(
        "agent",
        "example/gh/value",
        "example/docker/value",
        "example/app/id",
        "example/app/client",
        "example/app/pem",
        "example/signing/key",
    )
    return dataclasses.replace(
        original,
        credentials=(references,),
        sandbox=dataclasses.replace(
            original.sandbox,
            write_roots=(".cache", ".local/share/mise", ".local/state/mise"),
        ),
        kill_grace_seconds=0,
        claude=model.Claude(
            "primary", (model.ClaudeProfile("primary", "example/primary/token", None),)
        ),
    )


def constant[Result](value: Result) -> Callable[..., Result]:
    def fixed(*_args: object, **_kwargs: object) -> Result:
        return value

    return fixed


def bounded_file_reads(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    wrapped = os.fdopen

    @contextmanager
    def checked(fd: int, mode: str) -> Generator[SimpleNamespace]:
        assert mode == "rb"
        with wrapped(fd, mode) as stream:

            def read(count: int) -> bytes:
                assert count == size
                return stream.read(count)

            yield SimpleNamespace(fileno=stream.fileno, read=read)

    monkeypatch.setattr(os, "fdopen", checked)


def trusted_host_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[Path, tuple[int, int]]:
    """Model host ownership and modes without changing the filesystem owner."""
    overrides: dict[Path, tuple[int, int]] = {}
    original = host_tools.lstat

    def checked(path: Path) -> os.stat_result:
        info = original(path)
        uid, mode = overrides.get(path, (0, info.st_mode & ~0o022))
        values = list(info)
        values[0], values[4] = mode, uid
        return os.stat_result(values)

    monkeypatch.setattr(host_tools, "lstat", checked)
    return overrides
