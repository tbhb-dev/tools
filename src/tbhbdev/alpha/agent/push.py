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
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from tbhbdev.alpha.agent.credentials import PushTokenFailure
    from tbhbdev.alpha.agent.run_host import Prepared, Run

# A remote rejection is on a later stderr line than the URL, so keep all of it.
PUSH_ERROR_LIMIT = 4000
HISTORY_TIMEOUT = 30
STRANDED_WORK = 7


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
    status, _, error = git_with_token(
        workspace, token, "push", "--", target, f"HEAD:refs/heads/{branch}"
    )
    return status, error


def git_with_token(
    workspace: Path, token: str, *args: str, capture_output: bool = False
) -> tuple[int, str, str]:
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
            *args,
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
                    stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
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
        output, error = child.communicate()
        writer.join()
    return child.returncode, output or "", error


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
    before, _ = local_refs(workspace)
    base = before.get(f"refs/heads/{branch}")
    start = start_ref_target(run, base)
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
    return publish_changes(run, workspace, before, token, start)


def publish_changes(
    run: Run,
    workspace: Path,
    before: dict[str, str],
    token: str | PushTokenFailure | None,
    start: tuple[str, str] | None,
) -> int:
    reference = f"refs/heads/agent/{run.files.name}"
    try:
        after, _ = local_refs(workspace)
        commit, stranded = new_commits(run, workspace, before, after, reference)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        run.files.write(
            "log", f"push skipped: cannot check new commits ({type(exc).__name__})\n"
        )
        return 4
    if stranded:
        run.files.write("head", "".join(f"{sha} {ref}\n" for sha, ref, _ in stranded))
        for sha, ref, reason in stranded:
            run.files.write(
                "log", f"push skipped: stranded commit {sha} on {ref} {reason}\n"
            )
        return STRANDED_WORK
    if commit is None:
        return 0
    if start is not None:
        return push_start_branch(run, workspace, commit, token, start)
    return publish(run, workspace, commit, token, before.get(reference))


def publish(
    run: Run,
    workspace: Path,
    commit: str,
    token: str | PushTokenFailure | None,
    base: str | None,
) -> int:
    branch = f"agent/{run.files.name}"
    run.files.write("head", commit)
    if not isinstance(token, str):
        reason = token.reason if token is not None else "unavailable"
        run.files.write(
            "log", f"push skipped: no push token ({reason}) for commit {commit}\n"
        )
        return 4
    try:
        with publication(run.files.root, workspace, commit) as directory:
            published = open_pr.PublishedRange(workspace, directory, base, commit)
            # Without a pre-run commit, keep the existing PR refusal after push.
            if base is not None:
                open_pr.check_range(published)
            status, error = send(directory, url(run.request.repo), branch, token)
            if not status:
                run.files.write("branch", branch)
                return 0 if opened(run, published, branch, token) else 5
    except open_pr.OpenRefusedError as refusal:
        run.files.write("log", f"push refused: {refusal}\n")
        return 4
    except subprocess.TimeoutExpired:
        error = "git timed out"
    except OSError, subprocess.SubprocessError:
        error = "git push unavailable"
    text = " ".join(error.replace(token, "[redacted]").split()) or "git push failed"
    run.files.write("log", f"push failed: {text[:PUSH_ERROR_LIMIT]}\n")
    return 4


def start_ref_target(run: Run, base: str | None) -> tuple[str, str] | None:
    ref = run.request.start_ref
    if ref is None:
        return None
    if base is None:
        message = f"missing fetched starting commit for {ref}"
        raise Invalid(message)
    run.files.write("log", f"start ref: {ref} at {base}\n")
    return ref, base


def check_not_moved(
    directory: Path, target: str, ref: str, base: str, token: str
) -> None:
    status, listing, _ = git_with_token(
        directory, token, "-c", "http.followRedirects=false",
        "ls-remote", "--symref", "--exit-code", "--", target, "HEAD", ref,
        capture_output=True,
    )  # fmt: skip
    if status:
        message = f"cannot read remote tip of {ref}"
        raise Invalid(message)
    records = {
        name: value
        for value, _, name in (line.partition("\t") for line in listing.splitlines())
    }
    # HEAD can appear twice, once as a symref and once as a commit.
    default = re.search(r"^ref: (refs/heads/[^\s]+)\tHEAD$", listing, re.MULTILINE)
    if default is None or default[1] == ref:
        message = f"cannot verify {ref} is a non-default branch"
        raise Invalid(message)
    if records.get(ref) != base:
        message = f"remote branch {ref} moved from {base} or no longer exists"
        raise Invalid(message)


def push_start_branch(
    run: Run,
    workspace: Path,
    commit: str,
    token: str | PushTokenFailure | None,
    start: tuple[str, str],
) -> int:
    ref, base = start
    run.files.write("head", commit)
    if not isinstance(token, str):
        reason = token.reason if token is not None else "unavailable"
        run.files.write(
            "log", f"push failed: no push token ({reason}) for commit {commit}\n"
        )
        return 4
    try:
        target = url(run.request.repo)
        branch = ref.removeprefix("refs/heads/")
        with publication(run.files.root, workspace, commit) as directory:
            check_not_moved(directory, target, ref, base, token)
            status, error = send(directory, target, branch, token)
        if not status:
            run.files.write("branch", branch)
            run.files.write("log", f"pushed {ref} at {commit}\n")
            return 0
        error = (
            " ".join(error.replace(token, "[redacted]").split()) or "git push failed"
        )
    except Invalid as exc:
        error = str(exc)
    except subprocess.TimeoutExpired:
        error = "git timed out"
    except OSError, ValueError, subprocess.SubprocessError:
        error = "git push unavailable"
    run.files.write("log", f"push failed: {error[:PUSH_ERROR_LIMIT]}\n")
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
    reference: str,
) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Find the publishable tip and any new work it does not cover."""
    start = before.get(reference) or before.get("HEAD", before.get("detached HEAD"))
    bases = tuple(dict.fromkeys(before.values()))
    candidates = {
        name: sha
        for name, sha in after.items()
        if (name.startswith("refs/heads/") or name == "detached HEAD")
        and sha not in bases
    }
    if not start or not candidates:
        return None, []
    commit = None
    stranded: list[tuple[str, str, str]] = []
    with publication(
        run.files.root, workspace, next(iter(candidates.values()))
    ) as directory:
        tip = candidates.pop(reference, None)
        if tip is not None:
            if outside_history(directory, tip, bases, start):
                commit = tip
            elif outside_history(directory, tip, bases):
                stranded.append((tip, reference, f"outside start ancestry ({start})"))
            bases = (*bases, tip)
        for name, sha in sorted(candidates.items()):
            if outside_history(directory, sha, bases):
                ref = "".join(filter(str.isprintable, name))
                stranded.append((sha, ref, f"outside {reference}"))
    return commit, stranded


def outside_history(
    directory: Path, tip: str, bases: tuple[str, ...], start: str | None = None
) -> str:
    """Return a new commit SHA, optionally restricted to descendants of the start."""
    revisions = "\n".join((tip, *(f"^{base}" for base in bases))) + "\n"
    # Excluding the start also excludes its ancestors.
    ancestry = (f"--ancestry-path={start}",) if start else ()
    arguments = ("rev-list", *ancestry, "--max-count=1", "--stdin", "--")
    return history_git(directory, *arguments, revisions=revisions).strip()


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
