"""Fetch and create one worktree under the clone's stable git-directory lock."""

from __future__ import annotations

import os
import subprocess  # nosec B404
from pathlib import Path

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions import run_state
from tbhbdev.alpha.agent.model import Invalid, Profile, RunRequest


def git(clone: Path, *args: str) -> str:
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "SSH_AUTH_SOCK", "LANG")
        if key in os.environ
    }
    env.update(
        GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1"
    )
    return subprocess.check_output(  # nosec B603 B607
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.sshCommand=ssh",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "core.pager=cat",
            "-C",
            str(clone),
            *args,
        ],
        env=env,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def common_dir(clone: Path) -> Path:
    return Path(git(clone, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def create(clone: Path, name: str, request: RunRequest) -> Path:
    if not clone.is_dir():
        message = f"repo clone {clone} does not exist"
        raise Invalid(message)
    worktree = clone / ".worktrees" / f"agent-{name}"
    with runfiles.locked(common_dir(clone) / "agent-worktree.lock"):
        git(clone, "fetch", "origin")
        default = git(clone, "symbolic-ref", "refs/remotes/origin/HEAD")
        branch = default.removeprefix("refs/remotes/origin/")
        current = git(clone, "branch", "--show-current")
        if current == branch and not git(
            clone, "status", "--porcelain", "--untracked-files=no"
        ):
            git(clone, "merge", "--ff-only", default)
        worktree.parent.mkdir(exist_ok=True)
        if request.profile is Profile.BUILD:
            git(clone, "worktree", "add", "-b", f"agent/{name}", str(worktree), default)
        else:
            commit = git(clone, "rev-parse", "--verify", f"{request.commit}^{{commit}}")
            git(clone, "worktree", "add", "--detach", str(worktree), commit)
    return worktree


def remove(clone: Path, name: str, *, force: bool) -> None:
    """Remove a worktree and keep any branch with local-only commits."""
    with runfiles.locked(common_dir(clone) / "agent-worktree.lock"):
        path = clone / ".worktrees" / f"agent-{name}"
        branch = git(path, "branch", "--show-current")
        git(clone, "worktree", "remove", *(["--force"] if force else []), str(path))
        if branch:
            unpublished = bool(git(clone, "rev-list", branch, "--not", "--remotes"))
            if not run_state.keep_branch(unpublished=unpublished, force=force):
                git(clone, "branch", "-D", branch)
