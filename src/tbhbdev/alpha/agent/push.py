"""Record build commits and push updates to branches named by --start-ref."""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import tempfile
import threading
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import host_tools, runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from tbhbdev.alpha.agent.credentials import PushTokenFailure
    from tbhbdev.alpha.agent.run_host import Prepared, Run

# A remote rejection is on a later stderr line than the URL, so keep all of it.
PUSH_ERROR_LIMIT = 4000
HISTORY_TIMEOUT = 30


class PublicationRefusedError(ValueError):
    """The run has new history that cannot be recorded as its head."""


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
    reference = f"refs/heads/{branch}"
    before, _ = local_refs(workspace)
    base = before.get(reference)
    start = start_ref_target(run, base)
    rc = harness(run, prepared, stack)
    if run.files.uncertain:
        return rc
    if rc:
        with suppress(OSError, ValueError):
            commit = head(workspace, branch)
            if commit is not None and commit != base:
                run.files.write("head", commit)
                if start is not None:
                    run.files.write("log", f"push skipped: harness exited {rc}\n")
        return rc
    try:
        if start is not None:
            return publish_start_ref(run, workspace, before, token, start)
        after, checkout = local_refs(workspace)
        found = new_commits(run, workspace, before, after, checkout)
    except PublicationRefusedError as exc:
        run.files.write("log", f"commit check failed: {exc}\n")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        run.files.write(
            "log",
            f"commit check failed: cannot check new commits ({type(exc).__name__})\n",
        )
    else:
        if found:
            run.files.write("head", found[0][1])
        return 0
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


def publish_start_ref(
    run: Run,
    workspace: Path,
    before: dict[str, str],
    token: str | PushTokenFailure | None,
    start: tuple[str, str],
) -> int:
    branch = f"agent/{run.files.name}"
    reference = f"refs/heads/{branch}"
    after, _ = local_refs(workspace)
    after.pop("HEAD", None)
    commit = after.pop(reference, None)
    if commit is not None:
        if isinstance(token, str) and head(workspace, branch) is None:
            after[reference] = commit
        else:
            after = {reference: commit, **after}
    change = first_change(run, workspace, before, after)
    return (
        0 if change is None else push_start_branch(run, workspace, change, token, start)
    )


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
    change: tuple[str, str],
    token: str | PushTokenFailure | None,
    start: tuple[str, str],
) -> int:
    ref, base = start
    name, commit = change
    run_branch = f"agent/{run.files.name}"
    if name == f"refs/heads/{run_branch}":
        run.files.write("head", commit)
    if not isinstance(token, str):
        reason = token.reason if token is not None else "unavailable"
        run.files.write(
            "log", f"push failed: no push token ({reason}) for commit {commit}\n"
        )
        return 4
    try:
        if name != f"refs/heads/{run_branch}" or head(workspace, run_branch) != commit:
            run.files.write(
                "log", f"push failed: commit {commit} needs {run_branch} checked out\n"
            )
            return 4
        target = url(run.request.repo)
        branch = ref.removeprefix("refs/heads/")
        with publication(run.files.root, workspace, commit) as directory:
            if outside_history(directory, base, (commit,)):
                run.files.write(
                    "log",
                    f"push failed: commit {commit} does not fast-forward "
                    f"{ref} from {base}\n",
                )
                return 4
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


def first_change(
    run: Run, workspace: Path, before: dict[str, str], after: dict[str, str]
) -> tuple[str, str] | None:
    """Find a commit that no starting ref reaches."""
    bases = tuple(dict.fromkeys(before.values()))
    candidates = {name: tip for name, tip in after.items() if tip not in bases}
    if not candidates:
        return None
    with publication(
        run.files.root, workspace, next(iter(candidates.values()))
    ) as directory:
        for name, tip in candidates.items():
            if commit := outside_history(directory, tip, bases):
                return name, commit
    return None


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
        found = eligible_tips(directory, candidates, bases, start)
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


def eligible_tips(
    directory: Path, candidates: dict[str, str], bases: tuple[str, ...], start: str
) -> dict[str, str]:
    """Keep tips with new run history and refuse new history outside the start."""
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
    return found


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
