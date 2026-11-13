"""Push a completed run through a one-shot, inherited credential pipe."""

from __future__ import annotations

import os
import re
import stat
import subprocess  # nosec B404
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import private_names
from tbhbdev.alpha.agent import host_tools, runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.private_names import _configured

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from tbhbdev.alpha.agent.run_host import Prepared, Run


PRIVATE_NAMES = ".local/state/tbhb/private-repos.txt"


class PushRefusedError(ValueError):
    """The outgoing range could not pass the publication checks."""


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
    token: str | None,
) -> int:
    if token is None:
        return harness(run, prepared, stack)
    try:
        names = read_names(run.home / PRIVATE_NAMES)
        names.private.extend(_configured(strict=True))
    except (PushRefusedError, private_names.NameListError) as refusal:
        run.files.write("log", f"push refused: {refusal}\n")
        return 4
    workspace = Path(prepared.paths.worktree)
    branch = f"agent/{run.files.name}"
    before = head(workspace, branch)
    rc = harness(run, prepared, stack)
    if (
        run.files.uncertain
        or (commit := head(workspace, branch)) is None
        or commit == before
    ):
        return rc
    run.files.write("head", commit)
    if rc:
        run.files.write("log", f"push skipped: harness exited {rc}\n")
        return rc
    try:
        with publication(run.files.root, workspace, commit) as directory:
            scan(directory, before, commit, names, branch)
            status, error = send(directory, url(run.request.repo), branch, token)
    except PushRefusedError as refusal:
        run.files.write("log", f"push refused: {refusal}\n")
        return 4
    except OSError, subprocess.SubprocessError:
        status, error = True, "git push unavailable"
    if status:
        line = next(iter(error.splitlines()), "git push failed").replace(
            token, "[redacted]"
        )
        run.files.write("log", f"push failed: {line}\n")
    else:
        run.files.write("branch", branch)
    return 4 if status else rc


def read_names(source: Path) -> private_names.Names:
    try:
        with os.fdopen(
            os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
        ) as incoming:
            metadata = os.fstat(incoming.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022
            ):
                message = (
                    "private-name list must be owned, regular and protected from writes"
                )
                raise PushRefusedError(message)
            data = incoming.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                message = "private-name list exceeds 1 MiB"
                raise PushRefusedError(message)
        text = data.decode()
    except OSError, UnicodeError:
        text = ""
    names = [
        name
        for line in text.splitlines()
        if (name := line.strip()) and not name.startswith("#")
    ]
    if not names:
        message = f"no private-name list at {source}"
        raise PushRefusedError(message)
    path_only = {
        word.lower()
        for line in text.splitlines()
        if line.strip().startswith(private_names.PATH_HEADER)
        for word in line.strip().removeprefix(private_names.PATH_HEADER).split()
    }
    if any(
        re.fullmatch(r"[\w.-]+", name, re.ASCII) is None
        for name in [*names, *path_only]
    ):
        message = "invalid private-name list"
        raise PushRefusedError(message)
    return private_names.Names(names, path_only)


def scan(
    directory: Path,
    before: str | None,
    commit: str,
    names: private_names.Names,
    branch: str,
) -> None:
    if before is None:
        message = "no base commit"
        raise PushRefusedError(message)
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
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ASKPASS": "/bin/false",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": host_tools.search(),
        }
    )
    command = [
        host_tools.resolve("git"),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotePath=false",
        "-C",
        str(directory),
    ]
    try:
        objects = subprocess.run(  # nosec B603
            [*command, "rev-list", "--objects", f"{before}..{commit}"],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        # Raw objects include merge results, binary blobs and both identities.
        # List paths separately too: a renamed file can reuse an old blob or
        # tree. Separate diffs against every merge parent include result paths.
        result = subprocess.run(  # nosec B603
            [*command, "cat-file", "--batch"],
            cwd=directory,
            env=env,
            input=b"".join(
                line.partition(b" ")[0] + b"\n" for line in objects.splitlines()
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        paths = subprocess.run(  # nosec B603
            [
                *command,
                "log",
                "--format=",
                "--name-only",
                "-z",
                "-m",
                "--no-renames",
                "--no-ext-diff",
                "--no-text" + "conv",
                f"{before}..{commit}",
            ],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
    except OSError, subprocess.SubprocessError:
        message = "git object scan unavailable"
        raise PushRefusedError(message) from None
    lines = [
        f"refs/heads/{branch}",
        *(
            f"tree/{path.decode(errors='replace')}"
            for path in paths.split(b"\0")
            if path
        ),
        *result.stdout.decode(errors="replace").splitlines(),
    ]
    hits = private_names.private_name_problems(
        lines, names.private, path_only=names.path_only
    )
    if hits:
        message = f"{len(hits)} private-name hit(s)" + "".join(
            f"\nline {n}: {message}" for n, message in hits
        )
        raise PushRefusedError(message)


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
