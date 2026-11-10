"""Disposable git configurations that execute an observable monitor."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from agent_host_example import commit, git

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Generator

PLANTS = ("config", "gitlink", "gitdir", "modules", "worktree", "commondir")


def hook_write_probe(
    home: Path, codex_home: Path, root: Path
) -> tuple[str, Path, Path]:
    marker = root / "user-hook-marker"
    hook = f"printf invoked > {shlex.quote(str(marker))}"
    payload = json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook}]}]}}
    )
    account_config = home / ".codex"
    account_config.mkdir(mode=0o700, parents=True, exist_ok=True)
    (account_config / "hooks.json").write_text(payload)
    destination = codex_home / "hooks.json"
    return (
        f"printf %s {shlex.quote(payload)} > {shlex.quote(str(destination))}",
        marker,
        destination,
    )


@contextmanager
def account_hook_probe(
    home: Path, codex_home: Path, root: Path
) -> Generator[tuple[str, Path, Path]]:
    """Move existing hooks aside without reading them, and restore them afterward."""
    config = home / ".codex"
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        codex_home / "probe.lock",
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    with os.fdopen(descriptor, "ab") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        destination = codex_home / "hooks.json"
        if destination.exists() or destination.is_symlink():
            message = "the prepared Codex home must not contain hooks.json"
            raise Invalid(message)
        archive = Path(tempfile.mkdtemp(prefix="hook-probe-", dir=config))
        source = config / "hooks.json"
        previous = archive / "previous"
        if source.exists() or source.is_symlink():
            source.rename(previous)
        try:
            yield hook_write_probe(home, codex_home, root)
        finally:
            if source.exists() or source.is_symlink():
                source.rename(archive / "generated")
            if previous.exists() or previous.is_symlink():
                previous.rename(source)
            if destination.exists() or destination.is_symlink():
                rejected = Path(
                    tempfile.mkdtemp(prefix="rejected-hooks-", dir=codex_home)
                )
                destination.rename(rejected / "hooks.json")


def plant(workspace: Path, root: Path, name: str) -> tuple[Path, Path]:
    marker = root / "git-marker"
    script = root / "monitor.sh"
    script.write_text(f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\n")
    script.chmod(0o700)
    target = workspace
    if name in ("gitlink", "modules"):
        target = workspace / "nested"
        arguments = ["init", "--template="]
        if name == "modules":
            (workspace / ".git" / "modules").mkdir()
            arguments += [
                "--separate-git-dir",
                str(workspace / ".git" / "modules" / "x"),
            ]
        git(workspace, *arguments, str(target))
        head = commit(target, "nested fixture")
        git(workspace, "update-index", "--add", "--cacheinfo", f"160000,{head},nested")
    elif name in ("gitdir", "commondir"):
        original = workspace / ".git"
        common = root / "common.git"
        original.rename(common)
        if name == "gitdir":
            original.write_text(f"gitdir: {common}\n")
        else:
            original.mkdir()
            (original / "commondir").write_text(str(common) + "\n")
            (original / "HEAD").write_bytes((common / "HEAD").read_bytes())
            (original / "index").write_bytes((common / "index").read_bytes())
    if name == "worktree":
        git(target, "config", "extensions.worktreeConfig", "true")
        git(target, "config", "--worktree", "core.fsmonitor", str(script))
    else:
        git(target, "config", "core.fsmonitor", str(script))
    return marker, target
