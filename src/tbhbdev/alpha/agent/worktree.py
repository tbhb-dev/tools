"""Fetch and create one worktree under the clone's stable git-directory lock."""

from __future__ import annotations

import os
import subprocess  # nosec B404
from functools import partial
from pathlib import Path

from tbhbdev.alpha.agent import host_tools, runfiles
from tbhbdev.alpha.agent.model import Invalid, Profile, RunRequest

GIT_OVERRIDES = (
    "gc.auto=0",
    "maintenance.auto=false",
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.sshCommand=ssh",
    "protocol.ext.allow=never",
    "core.pager=cat",
    "credential.helper=",
    "core.askPass=",
    "protocol.file.allow=user",
    "remote.origin.uploadpack=git-upload-pack",
)


def git(clone: Path, *args: str) -> str:
    env = {
        key: os.environ[key]
        for key in ("HOME", "SSH_AUTH_SOCK", "LANG")
        if key in os.environ
    }
    env.update(
        PATH=host_tools.search(),
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_ASKPASS="/bin/false",
        SSH_ASKPASS="/bin/false",
    )
    command = [host_tools.resolve("git")]
    command += [item for value in GIT_OVERRIDES for item in ("-c", value)]
    command += ["-C", str(clone)]
    offline = ["-c", "protocol.allow=never"]
    invocation = [*command, *([] if args[0] == "fetch" else offline), *args]
    output = partial(
        subprocess.check_output,
        env=env,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    try:
        keys = output(
            [
                *command,
                *offline,
                "config",
                "--null",
                "--name-only",
                "--get-regexp",
                r"^(filter|merge)\.",
            ]
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        return output(invocation).strip()
    overrides = driver_overrides(keys)
    return output([*invocation[: -len(args)], *overrides, *args]).strip()


def driver_overrides(keys: str) -> list[str]:
    values: list[str] = []
    for key in dict.fromkeys(keys.split("\0")):
        group, _, field = key.rpartition(".")
        if group.startswith("filter."):
            values.extend(f"{group}.{name}=" for name in ("smudge", "clean", "process"))
            values.append(f"{group}.required=false")
        elif group.startswith("merge.") and field == "driver":
            values.append(f"{group}.driver=")
    return [item for value in dict.fromkeys(values) for item in ("-c", value)]


def common_dir(clone: Path) -> Path:
    return Path(git(clone, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def create(clone: Path, name: str, request: RunRequest) -> Path:
    if not clone.is_dir():
        message = f"repo clone {clone} does not exist"
        raise Invalid(message)
    worktree = clone / ".worktrees" / f"agent-{name}"
    with runfiles.locked(common_dir(clone) / "agent-worktree.lock"):
        git(clone, "fetch", "--upload-pack=git-upload-pack", "origin")
        default = git(clone, "symbolic-ref", "refs/remotes/origin/HEAD")
        worktree.parent.mkdir(exist_ok=True)
        if request.profile is Profile.BUILD:
            git(clone, "worktree", "add", "-b", f"agent/{name}", str(worktree), default)
        else:
            commit = git(clone, "rev-parse", "--verify", f"{request.commit}^{{commit}}")
            git(clone, "worktree", "add", "--detach", str(worktree), commit)
    return worktree
