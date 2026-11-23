"""Refuse protected-branch pushes and unfetched rewrites.

A non-fast-forward update to an unprotected branch is allowed only when the
remote tip git reports is the tip this clone fetched into
``refs/remotes/<remote>/<branch>``, the value ``--force-with-lease`` checks.
The hook cannot see the flag, so a plain force reports the same tip. The
command-line guard (``guard_git_hooks``) refuses plain force, and this hook
logs ``old -> new`` on stderr for every rewrite it allows.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.identity import AGENT_EMAIL
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Sequence

SAFE = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
)
HOOK_ARGUMENTS = 2
PROTECTED = frozenset({"refs/heads/main", "refs/heads/master"})
AGENT_KEY = Path(".config/tbhbdev/agent-signing-key.pub")
FINGERPRINT = re.compile(r"SHA256:\S+")
RANGE_LIMIT = 1000


def agent_fingerprint() -> str:
    """The fingerprint of the agent's public signing key, or empty if unreadable."""
    done = subprocess.run(  # nosec B603 B607
        ["ssh-keygen", "-l", "-f", str(Path.home() / AGENT_KEY)],
        capture_output=True,
        text=True,
        check=False,
    )
    found = FINGERPRINT.search(done.stdout)
    return found[0] if done.returncode == 0 and found else ""


def signature_fingerprint(raw: str) -> str:
    """The fingerprint of a commit's SSH signature, or empty if it has none."""
    head, _, _ = raw.partition("\n\n")
    lines = head.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("gpgsig ")), -1)
    if start < 0:
        return ""
    end = start + 1
    while end < len(lines) and lines[end].startswith(" "):
        end += 1
    signature = "\n".join(
        [lines[start].removeprefix("gpgsig "), *(x[1:] for x in lines[start + 1 : end])]
    )
    rest = "\n".join(lines[:start] + lines[end:]) + raw[len(head) :]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "signature"
        path.write_text(signature + "\n")
        done = subprocess.run(  # nosec B603 B607
            ["ssh-keygen", "-Y", "check-novalidate", "-n", "git", "-s", str(path)],
            input=rest,
            capture_output=True,
            text=True,
            check=False,
        )
    found = FINGERPRINT.search(done.stdout + done.stderr)
    return found[0] if found else ""


def agent_commit_problems(remote: str, ref: str, local: str) -> list[str]:
    """Name each agent commit the push adds that the agent key didn't sign."""
    code, out = git(
        *SAFE,
        "rev-list",
        f"--max-count={RANGE_LIMIT}",
        local,
        "--not",
        f"--remotes={remote}",
    )
    problems: list[str] = []
    expected = ""
    for sha in out.split() if code == 0 else []:
        _, text = git(*SAFE, "cat-file", "commit", sha)
        raw = text + "\n"  # git() strips the newline that ends a commit object
        idents = [
            x
            for x in raw.partition("\n\n")[0].split("\n")
            if x.startswith(("author ", "committer "))
        ]
        if not any(f"<{AGENT_EMAIL}>" in x for x in idents):
            continue
        expected = expected or agent_fingerprint()
        if not expected:
            return [f"{ref}: agent signing key unreadable; push refused"]
        if signature_fingerprint(raw) != expected:
            problems.append(
                f"{ref}: agent commit {sha} is not signed with the agent key; "
                "push refused"
            )
    return problems


def fetched_tip(remote: str, ref: str) -> str:
    """The tip of the remote-tracking branch for a pushed ref, or empty."""
    if not ref.startswith("refs/heads/"):
        return ""
    code, out = git(
        *SAFE,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/{remote}/{ref.removeprefix('refs/heads/')}^{{commit}}",
    )
    return "" if code else out


def push_problems(remote: str, lines: Sequence[str]) -> list[str]:
    problems: list[str] = []
    for line in lines:
        _, local, ref, old = line.split()
        if ref in PROTECTED:
            problems.append(
                f"{ref}: push to a protected branch refused; "
                "push a new branch and open a pull request"
            )
            continue
        if not local.strip("0"):
            continue
        problems.extend(agent_commit_problems(remote, ref, local))
        if old.strip("0") and git(*SAFE, "merge-base", "--is-ancestor", old, local)[0]:
            if fetched_tip(remote, ref) == old:
                print(
                    f"pre-push-guard: {ref} rewritten {old} -> {local}",
                    file=sys.stderr,
                )
            else:
                problems.append(
                    f"{ref}: non-fast-forward push refused; the remote tip is not "
                    "the fetched one, so fetch and push with --force-with-lease"
                )
    return problems


def main(argv: list[str], prog: str) -> int:
    if len(argv) != HOOK_ARGUMENTS:
        print(f"usage: {prog} REMOTE URL", file=sys.stderr)
        return 1
    for variable in ("SKIP", "PREK_SKIP"):
        if os.environ.get(variable):
            print(f"pre-push-guard: {variable} is set; push refused", file=sys.stderr)
            return 1
    remote = argv[0]
    try:
        problems = push_problems(remote, sys.stdin.read().splitlines())
    except ValueError:
        problems = ["pre-push-guard: cannot inspect push; push refused"]
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))
