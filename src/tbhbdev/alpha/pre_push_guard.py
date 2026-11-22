"""Refuse rewritten history and protected-branch pushes."""

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


def push_problems(lines: Sequence[str]) -> list[str]:
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
            problems.append(
                f"{ref}: non-fast-forward push refused; push a new branch instead"
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
    try:
        problems = push_problems(sys.stdin.read().splitlines())
    except ValueError:
        problems = ["pre-push-guard: cannot inspect push; push refused"]
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))
