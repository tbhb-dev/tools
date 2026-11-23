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
import sys
from typing import TYPE_CHECKING

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
