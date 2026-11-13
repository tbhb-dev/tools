"""Fetch a host-only mirror and prepare a fresh clone before launching a run."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import stat
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
"""
CHECKOUT_SCAN_DEPTH = 3


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
        cwd="/",
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
    state = runfiles.state_home(home)
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
    hooks.chmod(hooks.stat().st_mode | stat.S_IWUSR)
    prek = shlex.quote(host_tools.resolve("prek"))
    scripts: dict[Path, str] = {}
    for name in ("pre-commit", "pre-push", *(p[0] for p in install.IDENTITY_HOOKS)):
        hook = hooks / (
            f"{name}.prek" if name not in ("pre-commit", "pre-push") else name
        )
        scripts[hook] = f'#!/bin/sh\nexec {prek} hook-impl --hook-type {name} -- "$@"\n'
    for name, verb in install.IDENTITY_HOOKS:
        scripts[hooks / name] = install.SHIM.format(
            mark=install.SHIM_MARK, verb=verb, hook=name, origin=install.ORIGIN_GREP
        )
    for hook, content in scripts.items():
        if hook.exists():
            hook.chmod(hook.stat().st_mode | stat.S_IWUSR)
        hook.write_text(content)
    for entry in (*hooks.rglob("*"), hooks):
        executable = entry.is_dir() or entry in scripts or entry.stat().st_mode & 0o111
        entry.chmod(0o555 if executable else 0o444)


def is_main_checkout(directory: Path) -> bool:
    """Whether DIRECTORY has a git directory of its own.

    A ``.git`` directory without a non-empty ``HEAD`` is not one: a sandbox
    creates empty directories and files as the mount points of its read-only
    Git paths, and every repository has a ``HEAD`` that names a ref or a commit.
    """
    git_dir = directory / ".git"
    common = git_dir / "commondir"
    head = git_dir / "HEAD"
    return (
        git_dir.is_dir(follow_symlinks=False)
        and head.is_file()
        and head.stat().st_size > 0
        and (
            not common.exists()
            or (git_dir / common.read_text().strip()).resolve() == git_dir.resolve()
        )
    )


def _contained_checkouts(root: Path, workspace: Path | None) -> list[str]:
    found: list[str] = []
    for directory, children, _files in root.walk():
        if directory != workspace and is_main_checkout(directory):
            found.append(f"write root {root} contains main checkout {directory}")
        if len(directory.relative_to(root).parts) >= CHECKOUT_SCAN_DEPTH:
            children.clear()
        if ".git" in children:
            children.remove(".git")
    return found


SANDBOX_MOUNT_POINTS = (".git", ".agents", ".aws", ".codex")


def clear_mount_points(writes: tuple[str, ...]) -> None:
    """Remove the empty directories a sandbox leaves in a write root.

    The sandbox creates these names as protected mount points. An empty `.git`
    would make the next launch refuse the root as a main checkout. `rmdir`
    leaves any directory that holds something, and a symlink is never followed.
    """
    for root in writes:
        for name in SANDBOX_MOUNT_POINTS:
            entry = Path(root) / name
            if entry.is_dir(follow_symlinks=False):
                with contextlib.suppress(OSError):
                    entry.rmdir()


def validate_writes(
    workspace: Path, source: Path, writes: tuple[str, ...]
) -> tuple[str, ...]:
    """Report the write roots that overlap a main checkout, and refuse none.

    The decision of 2026-11-12 makes the directory write allowlist audit-only:
    the launcher records what it would have refused and starts the run.
    """
    clear_mount_points(writes)
    workspace, source = workspace.resolve(), source.resolve()
    own = workspace if workspace.parent == source / ".worktrees" else None
    found: list[str] = []
    for root in (workspace, *(Path(p).resolve() for p in writes)):
        for ancestor in (root, *root.parents):
            if ancestor == own:
                break
            if is_main_checkout(ancestor):
                found.append(f"write root {root} reaches main checkout {ancestor}")
                break
        if source.is_relative_to(root):
            found.append(f"write root {root} contains main checkout {source}")
        found.extend(_contained_checkouts(root, own))
    return tuple(found)


def validate_hooks(workspace: Path, writes: tuple[str, ...]) -> None:
    """Read every Git config scope with the host environment, before launch."""
    if not (workspace / ".git").is_dir():
        return
    # The final command override disables hooks for this host-side query. Drop
    # just that value from the result, retaining earlier command-scope values.
    values = subprocess.check_output(  # nosec B603
        [
            host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
            "config",
            "--path",
            "--null",
            "--get-all",
            "core.hooksPath",
        ],
        cwd=workspace,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    ).split("\0")[:-2]
    allowed = {(workspace / ".git/hooks").resolve(), Path("/dev/null")}
    for value in values:
        path = (workspace / value).resolve()
        if path in allowed:
            continue
        writable = next(
            (p for p in writes if path.is_relative_to(Path(p).resolve())), None
        )
        reason = "" if writable is None else f" under write root {writable}"
        message = f"core.hooksPath {path}{reason} is not the workspace hooks directory"
        raise Invalid(message)


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


def prepare_git(workspace: Path) -> None:
    directory = workspace / ".git"
    for name, content in (
        ("config.worktree", ""),
        ("info", None),
        ("modules", None),
        ("commondir", ".\n"),
    ):
        entry = directory / name
        if entry.is_symlink():
            message = f"unexpected git metadata: {name}"
            raise Invalid(message)
        match content:
            case None:
                if not entry.exists():
                    entry.mkdir()
                valid = entry.is_dir() and next(entry.iterdir(), None) is None
            case _:
                if not entry.exists():
                    entry.write_text(content)
                valid = entry.is_file() and entry.read_text() == content
        if not valid:
            message = f"unexpected git metadata: {name}"
            raise Invalid(message)


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
        prepare_git(worktree)
    return worktree
