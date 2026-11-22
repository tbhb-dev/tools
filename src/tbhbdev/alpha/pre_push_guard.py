"""Refuse protected-branch pushes and private names before a public push.

A non-fast-forward update to an unprotected branch is allowed only when the
remote tip git reports is the tip this clone fetched into
``refs/remotes/<remote>/<branch>``, the value ``--force-with-lease`` checks.
The hook cannot see the flag, so a plain force reports the same tip. The
command-line guard (``guard_git_hooks``) refuses plain force, and this hook
logs ``old -> new`` on stderr for every rewrite it allows.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha import private_names as pn
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
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class GuardError(Exception):
    """A git failure whose output must not disclose commit content."""


def read_git(*args: str) -> str:
    code, out = git(*SAFE, *args)
    if code:
        raise GuardError
    return out


def findings(text: str, names: pn.Names, sha: str, where: str) -> list[str]:
    return [
        f"{sha[:12]} {where}: {why}"
        for _, why in pn.private_name_problems(
            text.splitlines(),
            names.private,
            path_only=names.path_only,
            prefixes=names.prefixes,
        )
    ]


def added_lines(patch: str) -> list[tuple[str, int, str]]:
    """Return added text with its destination path and line number."""
    path = ""
    number: int | None = None
    added: list[tuple[str, int, str]] = []
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            number = None
        elif number is None and line.startswith("+++ "):
            raw = line[4:]
            path = ast.literal_eval(raw) if raw.startswith('"') else raw
            path = path.removeprefix("b/")
        elif match := HUNK.match(line):
            number = int(match[1])
        elif number is not None and line.startswith(("+", " ")):
            if line.startswith("+"):
                added.append((path, number, line[1:]))
            number += 1
    return added


def commit_problems(sha: str, names: pn.Names) -> list[str]:
    problems = findings(
        read_git("show", "-s", "--format=%B", sha), names, sha, "message"
    )
    paths = read_git(
        "diff-tree",
        "--root",
        "-r",
        "-m",
        "--no-commit-id",
        "--name-only",
        "--diff-filter=AR",
        "-z",
        "-M",
        sha,
    )
    for path in paths.split("\0"):
        problems.extend(findings(path, names, sha, "path"))
    patch = read_git(
        "-c",
        "core.quotePath=false",
        "show",
        "--format=",
        "-p",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--find-renames",
        "--diff-merges=first-parent",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        sha,
    )
    for path, number, line in added_lines(patch):
        problems.extend(findings(line, names, sha, f"{path!r}:{number}"))
    return problems


def new_commit_problems(
    remote: str, local: str, names: pn.Names, scanned: set[str]
) -> list[str]:
    problems: list[str] = []
    for sha in read_git("rev-list", local, "--not", f"--remotes={remote}").splitlines():
        if sha not in scanned:
            scanned.add(sha)
            problems.extend(commit_problems(sha, names))
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


def push_problems(remote: str, url: str, lines: Sequence[str]) -> list[str]:
    names = pn.names_for_repository(
        pn.repository_name(url or read_git("remote", "get-url", "--push", remote))
    )
    problems: list[str] = []
    scanned: set[str] = set()
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
        if names is None:
            continue
        problems.extend(new_commit_problems(remote, local, names, scanned))
    return problems


def main(argv: list[str], prog: str) -> int:
    if len(argv) != HOOK_ARGUMENTS:
        print(f"usage: {prog} REMOTE URL", file=sys.stderr)
        return 1
    for variable in ("SKIP", "PREK_SKIP"):
        if os.environ.get(variable):
            print(f"pre-push-guard: {variable} is set; push refused", file=sys.stderr)
            return 1
    remote, url = argv
    try:
        problems = push_problems(remote, url, sys.stdin.read().splitlines())
    except pn.NameListError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except GuardError, ValueError:
        problems = ["pre-push-guard: cannot inspect push; push refused"]
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))
