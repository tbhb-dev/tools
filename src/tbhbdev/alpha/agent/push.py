"""Push a completed run through a one-shot, inherited credential pipe."""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import tempfile
import threading
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import host_tools, open_pr, runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Profile

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from tbhbdev.alpha.agent.credentials import PushTokenFailure
    from tbhbdev.alpha.agent.run_host import Prepared, Run


def url(repository: str) -> str:
    host, owner, name = repo.parts(repository)
    if host != "github.com":
        message = "push requires github.com"
        raise ValueError(message)
    return f"https://{host}/{owner}/{name}.git"


def helper(fd: int) -> str:
    return (
        '!f() { [ "$1" = get ] || exit 1; protocol=; host=; '
        'while IFS= read -r line && [ -n "$line" ]; do case "$line" in '
        "protocol=https) protocol=yes ;; host=github.com) host=yes ;; esac; done; "
        '[ "$protocol:$host" = yes:yes ] || exit 1; '
        f"IFS= read -r token < /dev/fd/{fd} || exit 1; "
        'printf "username=x-access-token\\npassword=%s\\n\\n" "$token"; }; f'
    )


def feed(fd: int, token: str) -> None:
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
    except BrokenPipeError:
        pass


def send(workspace: Path, target: str, branch: str, token: str) -> tuple[int, str]:
    with ExitStack() as stack:
        read_fd, write_fd = os.pipe()
        pipe = stack.enter_context(os.fdopen(read_fd))
        writer = threading.Thread(target=feed, args=(write_fd, token))
        command = [
            host_tools.resolve("git"),
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={helper(read_fd)}",
            "-c",
            "credential.useHttpPath=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.askPass=",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(workspace),
            "push",
            "--",
            target,
            f"HEAD:refs/heads/{branch}",
        ]
        env = {
            "HOME": str(workspace),
            "XDG_CONFIG_HOME": str(workspace),
            "XDG_STATE_HOME": str(workspace),
            "XDG_CACHE_HOME": str(workspace),
            "XDG_DATA_HOME": str(workspace),
            "PATH": host_tools.search(),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ASKPASS": "/bin/false",
        }
        try:
            child = stack.enter_context(
                subprocess.Popen(  # nosec B603
                    command,
                    env=env,
                    cwd=workspace,
                    pass_fds=(pipe.fileno(),),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                )
            )
        except BaseException:
            os.close(write_fd)
            raise
        pipe.close()
        writer.start()
        _, error = child.communicate()
        writer.join()
    return child.returncode, error


def run(
    run: Run,
    prepared: Prepared,
    stack: ExitStack,
    harness: Callable[[Run, Prepared, ExitStack], int],
    token: str | PushTokenFailure | None,
) -> int:
    if run.request.profile is not Profile.BUILD:
        return harness(run, prepared, stack)
    workspace = Path(prepared.paths.worktree)
    branch = f"agent/{run.files.name}"
    before = local_heads(workspace)
    rc = harness(run, prepared, stack)
    if run.files.uncertain:
        return rc
    if rc:
        with suppress(OSError, ValueError):
            commit = head(workspace, branch)
            if commit is not None and commit != before.get(branch):
                run.files.write("head", commit)
                run.files.write("log", f"push skipped: harness exited {rc}\n")
        return rc
    after = local_heads(workspace)
    commit = after.pop(branch, None)
    if isinstance(token, str):
        commit = head(workspace, branch)
    if commit is not None:
        after = {branch: commit, **after}
    change = first_change(run, workspace, before, after)
    if change is None:
        return 0
    return publish(run, workspace, change, branch, token)


def publish(
    run: Run,
    workspace: Path,
    change: tuple[str, str],
    branch: str,
    token: str | PushTokenFailure | None,
) -> int:
    name, commit = change
    wrong_branch = name != branch
    if wrong_branch:
        run.files.write(
            "log", f"push skipped: commits are on {name}, expected {branch}\n"
        )
    else:
        run.files.write("head", commit)
    if not isinstance(token, str):
        reason = token.reason if token is not None else "unavailable"
        run.files.write("log", f"push skipped: no push token ({reason})\n")
        return 4
    if wrong_branch:
        return 4
    try:
        with publication(run.files.root, workspace, commit) as directory:
            status, error = send(directory, url(run.request.repo), branch, token)
            if not status:
                run.files.write("branch", branch)
                return 0 if opened(run, directory, commit, branch, token) else 4
    except OSError, subprocess.SubprocessError:
        error = "git push unavailable"
    line = next(iter(error.splitlines()), "git push failed").replace(
        token, "[redacted]"
    )
    run.files.write("log", f"push failed: {line}\n")
    return 4


def local_heads(workspace: Path) -> dict[str, str]:
    """Read local branch tips and detached HEAD without loading clone config."""
    directory = workspace / ".git"
    heads: dict[str, str] = {}
    with suppress(FileNotFoundError):
        packed = runfiles.read_owned(
            directory / "packed-refs", 1024 * 1024, "packed refs"
        )
        heads.update(
            (name, commit)
            for commit, name in re.findall(
                r"^([0-9a-f]{40}) refs/heads/(\S+)$", packed, re.MULTILINE
            )
        )
    base = directory / "refs" / "heads"
    for path in base.rglob("*"):
        if not path.is_dir() and path.suffix != ".lock":
            value = runfiles.read_owned(path, 1024, "run ref").strip()
            if re.fullmatch(r"[0-9a-f]{40}", value):
                heads[path.relative_to(base).as_posix()] = value
    current = runfiles.read_owned(directory / "HEAD", 1024, "run HEAD").strip()
    if re.fullmatch(r"[0-9a-f]{40}", current):
        heads["detached HEAD"] = current
    return heads


def first_change(
    run: Run, workspace: Path, before: dict[str, str], after: dict[str, str]
) -> tuple[str, str] | None:
    """Find the first tip with commits outside the starting local history."""
    bases = tuple(dict.fromkeys(before.values()))
    candidates = {name: tip for name, tip in after.items() if tip not in bases}
    if not candidates:
        return None
    with publication(
        run.files.root, workspace, next(iter(candidates.values()))
    ) as directory:
        for name, tip in candidates.items():
            if outside_history(directory, tip, bases):
                return name, tip
    return None


def outside_history(directory: Path, tip: str, bases: tuple[str, ...]) -> bool:
    """True when the tip has a commit that no starting tip reaches."""
    env = dict.fromkeys(
        (
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ),
        str(directory),
    )
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        PATH=host_tools.search(),
    )
    listed = subprocess.run(  # nosec B603
        [
            host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(directory),
            "rev-list",
            "--max-count=1",
            tip,
            "--not",
            *bases,
            "--",
        ],
        cwd=directory,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(listed.stdout.strip())


def opened(run: Run, git_dir: Path, commit: str, branch: str, token: str) -> bool:
    """Open the run's pull request and record it; False when it was not opened."""
    try:
        number = open_pr.open_pull_request(
            git_dir, commit, run.request.repo, branch, token
        )
    except (open_pr.OpenRefusedError, OSError) as refusal:
        run.files.write("log", f"pull request not opened: {refusal}\n")
        return False
    run.files.write("pr", f"{run.request.repo}#{number}")
    return True


def head(workspace: Path, branch: str) -> str | None:
    """Read refs as data; never load the sandbox's git configuration."""
    directory = workspace / ".git"
    if (
        runfiles.read_owned(directory / "HEAD", 1024, "run HEAD").strip()
        != f"ref: refs/heads/{branch}"
    ):
        return None
    reference = f"refs/heads/{branch}"
    try:
        value = runfiles.read_owned(directory / reference, 1024, "run ref").strip()
    except FileNotFoundError:
        packed = runfiles.read_owned(
            directory / "packed-refs", 1024 * 1024, "packed refs"
        )
        value = next(
            (
                line.split(" ")[0]
                for line in packed.splitlines()
                if line.endswith(f" {reference}")
            ),
            None,
        )
    if value is None:
        message = "missing run commit"
        raise ValueError(message)
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        message = "invalid run commit"
        raise ValueError(message)
    return value


@contextmanager
def publication(root: Path, workspace: Path, commit: str) -> Generator[Path]:
    """Use the clone's object data through host-owned, temporary git metadata.

    The protected run state holds the metadata, outside every sandbox write
    root. Git never loads a config, hook, ref or helper from the run clone.
    """
    with tempfile.TemporaryDirectory(dir=root, prefix="push-") as temporary:
        directory = Path(temporary)
        (directory / "refs").mkdir()
        objects = directory / "objects" / "info"
        objects.mkdir(parents=True)
        (objects / "alternates").write_text(str(workspace / ".git" / "objects") + "\n")
        (directory / "config").write_text(
            "[core]\nrepositoryformatversion = 0\nbare = true\n"
        )
        (directory / "HEAD").write_text(commit + "\n")
        yield directory
