"""Refuse protected-branch pushes, unfetched rewrites, and invalid agent branches.

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
from functools import cached_property
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.signing_parity import public_keys
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
AGENT_BRANCH_PREFIXES = ("refs/heads/agent/", "refs/heads/renovate/")
AGENT_KEY = Path(".config/tbhbdev/agent-signing-key.pub")
FINGERPRINT = re.compile(r"SHA256:\S+")
RANGE_LIMIT = 1000
# SHA-1 repositories write `gpgsig`, SHA-256 repositories `gpgsig-sha256`.
SIGNATURE_HEADER = re.compile(rb"gpgsig(-sha256)? ")


def key_fingerprint(path: str, public_key: str | None = None) -> str:
    done = subprocess.run(  # nosec B603 B607
        ["ssh-keygen", "-l", "-f", path],
        input=public_key,
        capture_output=True,
        text=True,
        check=False,
    )
    found = FINGERPRINT.search(done.stdout)
    return found[0] if done.returncode == 0 and found else ""


def agent_fingerprint() -> str:
    """The fingerprint of this host's public signing key, or empty if unreadable."""
    return key_fingerprint(str(Path.home() / AGENT_KEY))


class SigningKeys:
    """Load signing keys on demand, once per push across all refs."""

    @cached_property
    def local(self) -> str:
        return agent_fingerprint()

    @cached_property
    def registered(self) -> set[str] | None:
        try:
            return {
                found
                for key in public_keys(timeout=5)
                if (found := key_fingerprint("-", key + "\n"))
            }
        except OSError, HTTPException, ValueError, KeyError, TypeError:
            return None

    def refusal(self, signer: str, ref: str, sha: str) -> str:
        """Explain why a signature was refused, or return empty when it passed."""
        if signer and signer == self.local:
            return ""
        registered = self.registered
        if signer in (registered or ()):
            return ""
        fallback = (
            " (could not fetch GitHub signing keys, checked the local key only)"
            if registered is None
            else ""
        )
        return (
            f"{ref}: agent commit {sha} lacks a valid agent signature{fallback}; "
            "push refused"
        )


def signature_fingerprint(raw: bytes) -> str:
    """The fingerprint of a commit's SSH signature, or empty if it has none."""
    head, _, _ = raw.partition(b"\n\n")
    lines = head.split(b"\n")
    start = next((i for i, x in enumerate(lines) if SIGNATURE_HEADER.match(x)), -1)
    if start < 0:
        return ""
    end = start + 1
    while end < len(lines) and lines[end].startswith(b" "):
        end += 1
    signature = b"\n".join(
        [
            SIGNATURE_HEADER.sub(b"", lines[start]),
            *(x[1:] for x in lines[start + 1 : end]),
        ]
    )
    rest = b"\n".join(lines[:start] + lines[end:]) + raw[len(head) :]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "signature"
        path.write_bytes(signature + b"\n")
        done = subprocess.run(  # nosec B603 B607
            ["ssh-keygen", "-Y", "check-novalidate", "-n", "git", "-s", str(path)],
            input=rest,
            capture_output=True,
            check=False,
        )
    found = FINGERPRINT.search((done.stdout + done.stderr).decode(errors="replace"))
    return found[0] if done.returncode == 0 and found else ""


def remote_tips() -> list[str]:
    """The tips of every remote-tracking ref and tag a pushed branch builds on."""
    return git(
        *SAFE, "for-each-ref", "--format=%(objectname)", "refs/remotes", "refs/tags"
    )[1].split()


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


def commit_bytes(sha: str) -> bytes:
    """The commit object as git stores it, byte for byte."""
    done = subprocess.run(  # nosec B603 B607
        ["git", *SAFE, "cat-file", "commit", sha],
        capture_output=True,
        check=False,
    )
    return done.stdout


def agent_commit_problems(
    ref: str, local: str, exclude: list[str], keys: SigningKeys
) -> list[str]:
    """Check the destination branch and signatures for newly pushed agent commits.

    ``exclude`` holds every remote-tracking and tag tip and the old sha git reports. A
    failed listing or a range over RANGE_LIMIT refuses the push.
    """
    code, shas = pushed_commits(local, exclude)
    if code:
        return [f"{ref}: cannot list the commits to push; push refused"]
    if len(shas) > RANGE_LIMIT:
        return [f"{ref}: the push adds over {RANGE_LIMIT} commits; push refused"]
    problems: list[str] = []
    for sha in shas:
        raw = commit_bytes(sha)
        idents = [
            x
            for x in raw.partition(b"\n\n")[0].split(b"\n")
            if x.startswith((b"author ", b"committer "))
        ]
        if not any(f"<{AGENT_EMAIL}>".encode() in x for x in idents):
            continue
        if ref.startswith("refs/heads/") and not ref.startswith(AGENT_BRANCH_PREFIXES):
            return [f"{ref}: push refused; push to an agent/<name> branch instead"]
        if problem := keys.refusal(signature_fingerprint(raw), ref, sha):
            problems.append(problem)
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
    remote: str, row: tuple[str, str, str], tips: Sequence[str], keys: SigningKeys
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
    exclude = known_objects([*tips, old] if old.strip("0") else list(tips))
    problems = agent_commit_problems(ref, local, exclude, keys)
    if old.strip("0") and git(*SAFE, "merge-base", "--is-ancestor", old, local)[0]:
        if fetched_tip(remote, ref) == old:
            print(f"pre-push-guard: {ref} rewritten {old} -> {local}", file=sys.stderr)
        else:
            problems.append(
                f"{ref}: non-fast-forward push refused; the remote tip is not "
                "the fetched one, so fetch and push with --force-with-lease"
            )
    return problems


def push_problems(remote: str, lines: Sequence[str]) -> list[str]:
    rows: list[tuple[str, str, str]] = []
    for line in lines:
        _, local, ref, old = line.split()  # ValueError: main() refuses the push
        rows.append((local, ref, old))
    tips = remote_tips()
    keys = SigningKeys()
    return [
        problem for row in rows for problem in ref_problems(remote, row, tips, keys)
    ]


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
