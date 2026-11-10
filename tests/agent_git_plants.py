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
    account_config.mkdir(mode=0o700, parents=True, exist_ok=True)
    (account_config / "hooks.json").write_text(payload)
    destination = codex_home / "hooks.json"
    return (
        f"printf %s {shlex.quote(payload)} > {shlex.quote(str(destination))}",
        marker,
        destination,
    )


def plant(workspace: Path, root: Path, name: str) -> tuple[Path, Path]:
    marker, script = monitor(root)
    target = workspace
    if name in ("gitlink", "modules"):
        target = workspace / "nested"
        arguments = ["init", "--template="]
        if name == "modules":
            (workspace / ".git" / "modules").mkdir(exist_ok=True)
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
    if name == "config":
        git(target, "config", "diff.external", str(script))
        (target / "tracked").write_text("modified\n")
    return marker, target


def monitor(root: Path) -> tuple[Path, Path]:
    marker = root / "git-marker"
    script = root / "monitor.sh"
    script.write_text(f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\n")
    script.chmod(0o700)
    return marker, script


def parent_plant(workspace: Path, root: Path, name: str) -> tuple[Path, Path]:
    if name in PLANTS:
        return plant(workspace, root, name)
    marker, script = monitor(root)
    if name == "embedded-bare":
        target = workspace / "embedded.git"
        git(workspace, "clone", "--bare", "--template=", str(workspace), str(target))
        (target / "index").write_bytes((workspace / ".git/index").read_bytes())
        git(target, "config", "core.bare", "false")
        git(target, "config", "core.worktree", str(workspace))
        git(target, "config", "core.fsmonitor", str(script))
    elif name == "nested-config":
        nested = workspace / "nested"
        git(workspace, "init", "--template=", str(nested))
        (nested / ".gitattributes").write_text("*.txt filter=fixture\n")
        (nested / "tracked.txt").write_text("original\n")
        head = commit(nested, "nested filter fixture")
        git(workspace, "update-index", "--add", "--cacheinfo", f"160000,{head},nested")
        for key in ("core.fsmonitor", "diff.external", "filter.fixture.clean"):
            git(nested, "config", key, str(script))
        (nested / "tracked.txt").write_text("modified\n")
        target = workspace
    else:
        assert name == "worktrees-entry"
        git(workspace, "config", "extensions.worktreeConfig", "true")
        target = root / "linked"
        target.mkdir()
        entry = workspace / ".git/worktrees/planted"
        entry.mkdir(parents=True)
        (entry / "HEAD").write_bytes((workspace / ".git/HEAD").read_bytes())
        (entry / "index").write_bytes((workspace / ".git/index").read_bytes())
        (entry / "commondir").write_text("../..\n")
        (entry / "gitdir").write_text(str(target / ".git") + "\n")
        (entry / "config.worktree").write_text(f"[core]\nfsmonitor = {script}\n")
        (target / ".git").write_text(f"gitdir: {entry}\n")
        (target / "tracked").write_bytes((workspace / "tracked").read_bytes())
    return marker, target
