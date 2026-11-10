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

from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.decisions import request


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    ).strip()


def commit(path: Path, message: str) -> str:
    git(path, "add", ".")
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
    )
    return git(path, "rev-parse", "HEAD")


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
