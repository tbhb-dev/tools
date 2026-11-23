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


def remote_tips(url: str) -> list[str] | None:
    """The tips at the push destination from ``git ls-remote``, or None on failure.

    ``url`` is the address git hands the hook, so a ``pushurl`` that differs
    from the fetch URL is the one read. Git already applied its ``insteadOf``
    rules to ``url``, and ``ls-remote`` would apply them again. A URL that a
    rule still rewrites could read another repository, so it returns None.
    """
    code, rewritten = git(*SAFE, "ls-remote", "--get-url", url)
    if code or rewritten != url:
        return None
    code, out = git(*SAFE, "ls-remote", url)
    if code:
        return None
    return [line.split()[0] for line in out.splitlines() if line.split()]


def known_objects(shas: list[str]) -> list[str]:
    """The shas this clone holds, from one ``cat-file --batch-check`` call."""
    done = subprocess.run(  # nosec B603 B607
        ["git", *SAFE, "cat-file", "--batch-check"],
        input="".join(f"{sha}\n" for sha in shas),
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line.split()[0] for line in done.stdout.splitlines() if " missing" not in line
    ]


def pushed_commits(local: str, exclude: list[str]) -> tuple[int, list[str]]:
    """The commits reachable from ``local`` but not from ``exclude``, capped."""
    done = subprocess.run(  # nosec B603 B607
        ["git", *SAFE, "rev-list", f"--max-count={RANGE_LIMIT + 1}", "--stdin"],
        input="\n".join([local, "--not", *exclude]) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout.split()


def agent_commit_problems(ref: str, local: str, exclude: list[str]) -> list[str]:
    """Name each agent commit the push adds that the agent key didn't sign.

    ``exclude`` holds the remote's current tips and the old shas git reports. A
    failed listing or a range over RANGE_LIMIT refuses the push.
    """
    code, shas = pushed_commits(local, exclude)
    if code:
        return [f"{ref}: cannot list the commits to push; push refused"]
    if len(shas) > RANGE_LIMIT:
        return [f"{ref}: the push adds over {RANGE_LIMIT} commits; push refused"]
    problems: list[str] = []
    expected = ""
    for sha in shas:
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


def ref_problems(
    remote: str, row: tuple[str, str, str], tips: Sequence[str]
) -> list[str]:
    """The refusals for one pushed ref: LOCAL, REF, and the remote's OLD value."""
    local, ref, old = row
    if ref in PROTECTED:
        message = (
            "push to a protected branch refused; "
            "push a new branch and open a pull request"
        )
        return [f"{ref}: {message}"]
    if not local.strip("0"):
        return []
    exclude = known_objects([*tips, *([old] if old.strip("0") else [])])
    problems = agent_commit_problems(ref, local, exclude)
    if old.strip("0") and git(*SAFE, "merge-base", "--is-ancestor", old, local)[0]:
        if fetched_tip(remote, ref) == old:
            print(f"pre-push-guard: {ref} rewritten {old} -> {local}", file=sys.stderr)
        else:
            problems.append(
                f"{ref}: non-fast-forward push refused; the remote tip is not "
                "the fetched one, so fetch and push with --force-with-lease"
            )
    return problems


def push_problems(remote: str, url: str, lines: Sequence[str]) -> list[str]:
    rows: list[tuple[str, str, str]] = []
    for line in lines:
        _, local, ref, old = line.split()  # ValueError: main() refuses the push
        rows.append((local, ref, old))
    tips = remote_tips(url) if any(local.strip("0") for local, _, _ in rows) else []
    if tips is None:
        return ["pre-push-guard: cannot read the remote's tips; push refused"]
    return [problem for row in rows for problem in ref_problems(remote, row, tips)]


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
        problems = push_problems(remote, argv[1], sys.stdin.read().splitlines())
    except ValueError:
        problems = ["pre-push-guard: cannot inspect push; push refused"]
    for problem in problems:
        print(problem, file=sys.stderr)
    return int(bool(problems))
