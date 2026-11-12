"""Push a completed run through a one-shot, inherited credential pipe."""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha import identity, private_names
from tbhbdev.alpha.agent import host_tools, runfiles
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid
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
        f"IFS= read -r token </dev/fd/{fd} || exit 1; "
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
    workspace = Path(prepared.paths.worktree)
    branch = f"agent/{run.files.name}"
    before = head(workspace, branch)
    rc = harness(run, prepared, stack)
    if run.files.uncertain:
        return rc
    commit = head(workspace, branch)
    if commit is None or commit == before:
        return rc
    run.files.write("head", commit)
    if rc:
        run.files.write("log", f"push skipped: harness exited {rc}\n")
        return rc
    try:
        names = read_names(run.home / PRIVATE_NAMES)
        with publication(run.files.root, workspace, commit) as directory:
            scan(directory, before, commit, names)
            secret_scan(directory, before, commit)
            commit_policy(directory, before, commit, agent_public_key(run.tmpdir))
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
        text = runfiles.read_owned(source, 1024 * 1024, "private-name list")
    except OSError, ValueError:
        text = ""
    names = private_names.parse_list(text.splitlines())
    if not names.private:
        message = f"no private-name list at {source}"
        raise PushRefusedError(message)
    return names


def _git_output(directory: Path, *arguments: str) -> str:
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
    try:
        result = subprocess.run(  # nosec B603
            [
                host_tools.resolve("git"),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "diff.external=",
                "-c",
                "core.pager=cat",
                "-C",
                str(directory),
                *arguments,
            ],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        message = "git log unavailable"
        raise PushRefusedError(message) from None
    return result.stdout


def scan(
    directory: Path, before: str | None, commit: str, names: private_names.Names
) -> None:
    if before is None:
        message = "no base commit"
        raise PushRefusedError(message)
    if _git_output(directory, "rev-list", "--merges", f"{before}..{commit}").strip():
        message = "merge commit in the pushed range"
        raise PushRefusedError(message)
    if _git_output(directory, "rev-list", f"{commit}..{before}").strip():
        message = "pushed head is not a fast-forward of the branch"
        raise PushRefusedError(message)
    output = _git_output(
        directory,
        "log",
        "--format=commit %H%n%B",
        "-p",
        "--no-ext-diff",
        "--no-textconv",
        f"{before}..{commit}",
    )
    lines = output.splitlines()
    known = names.private + list(_configured())
    texts = [
        (1, lines),
        *((number, [text]) for number, text in private_names.joined_literals(lines)),
    ]
    hits = sorted(
        {
            hit
            for first, group in texts
            for hit in private_names.private_name_problems(
                group, known, first, path_only=names.path_only
            )
        }
    )
    if hits:
        message = f"{len(hits)} private-name hit(s)" + "".join(
            f"\nline {n}: {message}" for n, message in hits
        )
        raise PushRefusedError(message)


def secret_scan(directory: Path, before: str | None, commit: str) -> None:
    """Scan the pushed range for secrets with the host's gitleaks."""
    try:
        result = subprocess.run(  # nosec B603
            [
                host_tools.resolve("gitleaks"),
                "git",
                f"--log-opts={before}..{commit}",
                "--redact",
                "--no-banner",
                "--exit-code=1",
                str(directory),
            ],
            cwd=directory,
            env={
                "HOME": str(directory),
                "PATH": host_tools.search(),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Invalid, OSError, subprocess.SubprocessError:
        message = "secret scan unavailable"
        raise PushRefusedError(message) from None
    if result.returncode == 1:
        message = "secret scan found a leak in the pushed range"
        raise PushRefusedError(message)
    if result.returncode:
        message = "secret scan failed"
        raise PushRefusedError(message)


def agent_public_key(tmpdir: Path) -> str:
    """Read the run's signing agent for the public key that signs its commits."""
    try:
        result = subprocess.run(  # nosec B603
            [host_tools.resolve("ssh-add"), "-L"],
            env={"SSH_AUTH_SOCK": str(tmpdir / "ssh.sock")},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            check=False,
        )
    except Invalid, OSError, subprocess.SubprocessError:
        result = None
    line = result.stdout.splitlines()[:1] if result and not result.returncode else []
    if not line:
        message = "signing key unavailable"
        raise PushRefusedError(message)
    return line[0]


def commit_policy(
    directory: Path, before: str | None, commit: str, public_key: str
) -> None:
    """Every pushed commit is signed by the agent's key and carries its identity."""
    signers = directory / "allowed_signers"
    signers.write_text(f'* namespaces="git" {public_key}\n')
    statuses = _git_output(
        directory,
        "-c",
        "gpg.format=ssh",
        "-c",
        f"gpg.ssh.allowedSignersFile={signers}",
        "log",
        "--format=%H %G?",
        f"{before}..{commit}",
    ).splitlines()
    reasons = []
    for line in statuses:
        sha, _, status = line.partition(" ")
        short = sha[:7]
        if status != "G":
            reasons.append(f"{short} is not signed by the agent's key")
        headers, _, text = _git_output(directory, "cat-file", "commit", sha).partition(
            "\n\n"
        )
        values: dict[str, str] = {}
        for header in headers.split("\n"):
            key, _, value = header.partition(" ")
            values.setdefault(key, value)
        reasons.extend(
            f"{short} {role} email is not the agent's"
            for role in ("author", "committer")
            if identity.ident_email(values.get(role, "")) != identity.AGENT_EMAIL
        )
        if identity.SIGNOFF.search(text):
            reasons.append(f"{short} carries a Signed-off-by or Co-authored-by trailer")
    if reasons:
        message = "commit policy: " + "; ".join(reasons)
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
