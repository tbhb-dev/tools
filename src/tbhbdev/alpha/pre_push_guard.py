"""Refuse rewritten history and private names before a public push."""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
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
GIT_PATHS = 3
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
            text.splitlines(), names.private, path_only=names.path_only
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
            problems.append(
                f"{ref}: non-fast-forward push refused; push a new branch instead"
            )
        if names is None:
            continue
        problems.extend(new_commit_problems(remote, local, names, scanned))
    return problems


def linked_worktree() -> Path | None:
    """Return the top of the current linked worktree, or None in a main checkout."""
    code, out = git(
        *SAFE,
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        "--git-common-dir",
        "--show-toplevel",
    )
    lines = out.splitlines()
    if code or len(lines) != GIT_PATHS or lines[0] == lines[1]:
        return None
    return Path(lines[2])


def bazelisk_path() -> str | None:
    """Find bazelisk on the absolute PATH entries only."""
    dirs = [
        d for d in os.environ.get("PATH", "").split(os.pathsep) if Path(d).is_absolute()
    ]
    return shutil.which("bazelisk", path=os.pathsep.join(dirs))


def stop_bazel() -> None:
    """Ask a linked worktree's Bazel server to stop, without waiting for it."""
    top = linked_worktree()
    if top is None:
        return
    try:
        exe = bazelisk_path()
        if exe is None or not (top / "MODULE.bazel").is_file():
            return
        subprocess.Popen(  # nosec B603
            [exe, "shutdown"],
            cwd=top,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return


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
    if not problems:
        stop_bazel()
    return int(bool(problems))
