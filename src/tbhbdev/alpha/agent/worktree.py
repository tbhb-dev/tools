"""Fetch a host-only mirror and prepare a fresh clone before launching a run."""

from __future__ import annotations

import os
import re
import shlex
import subprocess  # nosec B404
from functools import partial
from pathlib import Path

from tbhbdev.alpha import install
from tbhbdev.alpha.agent import host_tools, runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid, Profile, RunRequest

GIT_OVERRIDES = (
    "gc.auto=0",
    "transfer.fsckObjects=true",
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
    "fetch.recurseSubmodules=false",
    "submodule.recurse=false",
)
MIRROR_CONFIG = """\
[core]
repositoryformatversion = 0
bare = true
logAllRefUpdates = false
[gc]
auto = 0
pruneExpire = never
[maintenance]
auto = false
[fetch]
recurseSubmodules = false
[submodule]
recurse = false
[remote "origin"]
url = {url}
fetch = +refs/heads/agent/*:refs/heads/agent/*
"""


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
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_ASKPASS="/bin/false",
        SSH_ASKPASS="/bin/false",
    )
    command = [host_tools.resolve("git")]
    command += [item for value in GIT_OVERRIDES for item in ("-c", value)]
    directory = clone / ".git"
    command += (
        ["--git-dir", str(directory), "--work-tree", str(clone)]
        if directory.is_dir()
        else ["--git-dir", str(clone)]
    )
    offline = ["-c", "protocol.allow=never"]
    online = args[0] in ("fetch", "ls-remote")
    invocation = [*command, *([] if online else offline), *args]
    output = partial(
        subprocess.check_output,
        env=env,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    timeout = 300 if args[0] in ("fetch", "clone") else 30
    if args[0] == "init":
        return output(invocation).strip()
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
        return output(invocation, timeout=timeout).strip()
    overrides = driver_overrides(keys)
    return output(
        [*invocation[: -len(args)], *overrides, *args], timeout=timeout
    ).strip()


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


def mirror_path(home: Path, argument: str) -> Path:
    host, owner, name = repo.parts(argument)
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    return state / "tbhbdev" / "agent-mirrors" / host.lower() / owner / f"{name}.git"


def configure_mirror(mirror: Path, url: str) -> None:
    if not (mirror / "config").exists():
        git(mirror, "init", "--bare", "--template=")
    # All values come from validated repository names, never a checkout.
    runfiles.atomic(
        mirror / "config",
        MIRROR_CONFIG.format(url=url),
    )


def default_branch(mirror: Path) -> str:
    listing = git(mirror, "ls-remote", "--symref", "--end-of-options", "origin", "HEAD")
    for line in listing.splitlines():
        match = re.fullmatch(r"ref: (refs/heads/[^\s]+)\tHEAD", line)
        if match is not None:
            branch = match[1]
            git(
                mirror,
                "check-ref-format",
                "--branch",
                branch.removeprefix("refs/heads/"),
            )
            return branch
    message = "origin HEAD does not name a branch"
    raise Invalid(message)


def fetch_commit(mirror: Path, request: RunRequest) -> str:
    options = (
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--no-tags",
        "--upload-pack=git-upload-pack",
    )
    # Explicit ref mappings keep pruning confined to the agent namespace.
    git(
        mirror,
        "fetch",
        *options,
        "--prune",
        "origin",
        "+refs/heads/agent/*:refs/heads/agent/*",
    )
    branch = default_branch(mirror)
    references = [f"+{branch}:{branch}"]
    if request.commit is not None:
        references.append(f"+{request.commit}:refs/heads/tbhbdev-review")
    git(mirror, "fetch", *options, "--no-prune", "origin", *references)
    reference = branch if request.profile is Profile.BUILD else request.commit
    commit = git(
        mirror, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"
    )
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit) is None:
        message = "mirror did not resolve a full commit"
        raise Invalid(message)
    return commit


def install_hooks(workspace: Path) -> None:
    hooks = workspace / ".git" / "hooks"
    hooks.mkdir(mode=0o700, exist_ok=True)
    prek = shlex.quote(host_tools.resolve("prek"))
    for name in ("pre-commit", "pre-push", *(p[0] for p in install.IDENTITY_HOOKS)):
        hook = hooks / (
            f"{name}.prek" if name not in ("pre-commit", "pre-push") else name
        )
        hook.write_text(
            f'#!/bin/sh\nexec {prek} hook-impl --hook-type {name} -- "$@"\n'
        )
        hook.chmod(0o755)
    for name, verb in install.IDENTITY_HOOKS:
        hook = hooks / name
        hook.write_text(
            install.SHIM.format(
                mark=install.SHIM_MARK, verb=verb, hook=name, origin=install.ORIGIN_GREP
            )
        )
        hook.chmod(0o755)


def clone_run(mirror: Path, workspace: Path, url: str) -> None:
    # A copying transport avoids shared object inodes and readable alternates.
    workspace.mkdir(mode=0o700)
    git(
        mirror,
        "clone",
        "--no-local",
        "--no-hardlinks",
        "--no-checkout",
        "--template=",
        str(mirror),
        str(workspace),
    )
    git(workspace, "config", "remote.origin.url", url)
    git(workspace, "config", "remote.origin.pushurl", url)
    git(workspace, "config", "core.hooksPath", str(workspace / ".git" / "hooks"))


def create(clone: Path, name: str, request: RunRequest) -> Path:
    if not clone.is_dir():
        message = f"repo clone {clone} does not exist"
        raise Invalid(message)
    worktree = clone / ".worktrees" / f"agent-{name}"
    if worktree.parent.is_symlink():
        message = "run clone parent must not be a symbolic link"
        raise Invalid(message)
    mirror = mirror_path(Path.home(), request.repo)
    if any(p.is_symlink() for p in (mirror, *mirror.parents[:3])):
        message = "host mirror paths must not be symbolic links"
        raise Invalid(message)
    mirror = runfiles.private_dir(mirror)
    url = repo.ssh_url(request.repo)
    with runfiles.locked(mirror / "agent-mirror.lock"):
        configure_mirror(mirror, url)
        commit = fetch_commit(mirror, request)
        worktree.parent.mkdir(exist_ok=True)
        clone_run(mirror, worktree, url)
        if request.profile is Profile.BUILD:
            git(worktree, "checkout", "-b", f"agent/{name}", commit, "--")
        else:
            git(worktree, "checkout", "--detach", commit, "--")
        install_hooks(worktree)
    return worktree
