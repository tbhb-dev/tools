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

# A remote rejection is on a later stderr line than the URL, so keep all of it.
PUSH_ERROR_LIMIT = 4000
HISTORY_TIMEOUT = 30


class PublicationRefusedError(ValueError):
    """The run has new history that cannot be published safely."""


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
    reference = f"refs/heads/{branch}"
    before, _ = local_refs(workspace)
    base = before.get(reference)
    rc = harness(run, prepared, stack)
    if run.files.uncertain:
        return rc
    if rc:
        with suppress(OSError, ValueError):
            commit = head(workspace, branch)
            if commit is not None and commit != base:
                run.files.write("head", commit)
                run.files.write("log", f"push skipped: harness exited {rc}\n")
        return rc
    try:
        after, checkout = local_refs(workspace)
        found = new_commits(run, workspace, before, after, checkout)
    except PublicationRefusedError as exc:
        run.files.write("log", f"push skipped: {exc}\n")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        run.files.write(
            "log", f"push skipped: cannot check new commits ({type(exc).__name__})\n"
        )
    else:
        return publish(run, workspace, found, token, base) if found else 0
    return 4


def publish(
    run: Run,
    workspace: Path,
    found: list[tuple[str, str]],
    token: str | PushTokenFailure | None,
    base: str | None,
) -> int:
    branch = f"agent/{run.files.name}"
    commits = dict.fromkeys(commit for _, commit in found)
    if len(commits) > 1:
        sources = ", ".join(name.removeprefix("refs/heads/") for name, _ in found)
        sources = "".join(filter(str.isprintable, sources))
        run.files.write(
            "log",
            f"push skipped: commits {' '.join(commits)} on {sources} "
            f"need one branch, {branch}\n",
        )
        return 4
    commit = found[0][1]
    run.files.write("head", commit)
    if not isinstance(token, str):
        reason = token.reason if token is not None else "unavailable"
        run.files.write(
            "log", f"push skipped: no push token ({reason}) for commit {commit}\n"
        )
        return 4
    try:
        with publication(run.files.root, workspace, commit) as directory:
            status, error = send(directory, url(run.request.repo), branch, token)
            if not status:
                run.files.write("branch", branch)
                published = open_pr.PublishedRange(workspace, directory, base, commit)
                return 0 if opened(run, published, branch, token) else 5
    except subprocess.TimeoutExpired:
        error = "git timed out"
    except OSError, subprocess.SubprocessError:
        error = "git push unavailable"
    text = " ".join(error.replace(token, "[redacted]").split()) or "git push failed"
    run.files.write("log", f"push failed: {text[:PUSH_ERROR_LIMIT]}\n")
    return 4


def local_refs(workspace: Path) -> tuple[dict[str, str], str]:
    """Read loose and packed refs without loading the run's Git config."""
    directory = workspace / ".git"
    refs: dict[str, str] = {}
    with suppress(FileNotFoundError):
        packed = runfiles.read_owned(
            directory / "packed-refs", 1024 * 1024, "packed refs"
        )
        refs.update(
            (name, commit)
            for commit, name in re.findall(
                r"^([0-9a-f]{40}) (refs/\S+)$", packed, re.MULTILINE
            )
        )
    base = directory / "refs"
    for path in base.rglob("*"):
        if not path.is_dir() and path.suffix != ".lock":
            value = runfiles.read_owned(path, 1024, "run ref").strip()
            if re.fullmatch(r"[0-9a-f]{40}", value):
                refs[path.relative_to(directory).as_posix()] = value
    current = runfiles.read_owned(directory / "HEAD", 1024, "run HEAD").strip()
    if re.fullmatch(r"[0-9a-f]{40}", current):
        refs["detached HEAD"] = current
    elif tip := refs.get(current.removeprefix("ref: ")):
        refs["HEAD"] = tip
    return refs, current


def new_commits(
    run: Run,
    workspace: Path,
    before: dict[str, str],
    after: dict[str, str],
    checkout: str,
) -> list[tuple[str, str]]:
    """Find independent local branch or detached HEAD tips with new run history."""
    reference = f"refs/heads/agent/{run.files.name}"
    # Use the starting tip of the run's branch, falling back to the starting HEAD.
    start = before.get(reference) or before.get("HEAD", before.get("detached HEAD"))
    # Preparation fetches into the clone before the harness can change its refs.
    bases = tuple(dict.fromkeys(before.values()))
    candidates = {
        name: tip
        for name, tip in after.items()
        if (name.startswith("refs/heads/") or name == "detached HEAD")
        and tip not in bases
    }
    if not start or not candidates:
        return []
    with publication(
        run.files.root, workspace, next(iter(candidates.values()))
    ) as directory:
        found: dict[str, str] = {}
        dropped: set[str] = set()
        for name, tip in sorted(candidates.items()):
            if outside_history(directory, tip, bases, start):
                found[name] = tip
            elif outside_history(directory, tip, bases):
                dropped.add(tip)
        if dropped:
            short = " ".join(tip[:12] for tip in sorted(dropped))
            message = f"{len(dropped)} new tip(s) outside run start ancestry: {short}"
            raise PublicationRefusedError(message)
        if found and checkout == f"ref: {reference}":
            if reference in found:
                return [(reference, found[reference])]
            message = "checked-out run branch has no new commits"
            raise PublicationRefusedError(message)
        if len(found) <= 1:
            return list(found.items())
        independent = set(
            history_git(
                directory, "merge-base", "--independent", *found.values()
            ).split()
        )
        return [(name, tip) for name, tip in found.items() if tip in independent]


def outside_history(
    directory: Path, tip: str, bases: tuple[str, ...], start: str | None = None
) -> bool:
    """Check for new history, optionally restricted to descendants of the start."""
    revisions = "\n".join((tip, *(f"^{base}" for base in bases))) + "\n"
    # Excluding the start also excludes its ancestors.
    ancestry = (f"--ancestry-path={start}",) if start else ()
    return bool(
        history_git(
            directory,
            "rev-list",
            *ancestry,
            "--max-count=1",
            "--stdin",
            "--",
            revisions=revisions,
        ).strip()
    )


def history_git(directory: Path, *args: str, revisions: str | None = None) -> str:
    """Run a Git history query with the protected publication metadata."""
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
    return subprocess.run(  # nosec B603
        [
            host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(directory),
            *args,
        ],
        cwd=directory,
        env=env,
        input=revisions,
        capture_output=True,
        text=True,
        check=True,
        timeout=HISTORY_TIMEOUT,
    ).stdout


def opened(
    run: Run, published: open_pr.PublishedRange, branch: str, token: str
) -> bool:
    """Open the run's pull request and record it; False when it was not opened."""
    try:
        number = open_pr.open_pull_request(
            published, run.request.repo, branch, token, run.files.write
        )
    except (open_pr.OpenRefusedError, OSError) as refusal:
        reason = (
            str(refusal)
            if isinstance(refusal, open_pr.OpenRefusedError)
            else type(refusal).__name__
        )
        run.files.write("log", f"pr not opened: {' '.join(reason.split())}\n")
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
