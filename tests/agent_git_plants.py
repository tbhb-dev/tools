"""Disposable git configurations that execute an observable monitor."""

from __future__ import annotations

import json
import shlex
lazy from pathlib import Path

from agent_host_example import commit, git

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
    account_config.mkdir(parents=True)
    (account_config / "hooks.json").write_text(payload)
    destination = codex_home / "hooks.json"
    return (
        f"printf %s {shlex.quote(payload)} > {shlex.quote(str(destination))}",
        marker,
        destination,
    )


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
