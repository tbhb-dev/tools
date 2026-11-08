# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""``tbhbdev alpha pr close`` and ``tbhbdev alpha issue close``.

Both close through the agent's own ``gh``, as the agent's GitHub account, and
leave a final comment that holds the reason and the posting agent's
``Generated-by`` trailer. Each refuses anything that isn't open, and an
invoker that isn't a recognized agent session, because the trailer names one.

Provisional, until @tbhb decides open question 11 of the alpha-review
proposal: a session closes a pull request it opened freely, which the opened
ledger decides. It closes another agent's pull request only with a reason,
which the comment records along with the fact that the closer isn't the opener.
A pull request with no ``Opened-by`` line, such as a human's, is refused. Any
session may close an issue with a reason.
"""

import argparse
import os
import shlex
import sys
import urllib.parse
from typing import TYPE_CHECKING

from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import opened
from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    import subprocess  # nosec B404
    from collections.abc import Mapping, Sequence

REASONS = {"completed": "completed", "not-planned": "not planned"}


def gh(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run gh with the arguments."""
    return pr.run([pr.find_tool(pr.GH), *argv])


def view(kind: str, number: int) -> dict[str, object]:
    """The ``state``, ``url`` and ``body`` of a pull request or an issue.

    A pull request also reports ``headRefName`` and ``isCrossRepository``.

    Raises:
        SystemExit: gh can't read it, or it isn't open.
    """
    fields = "state,url,body"
    if kind == "pr":
        fields += ",headRefName,isCrossRepository"
    done = gh([kind, "view", str(number), "--json", fields])
    data = pr.gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read {kind} {number}: {why}"
        raise SystemExit(message)
    if data.get("state") != "OPEN":
        message = f"not closing {kind} {number}: it is {data.get('state')}, not OPEN"
        raise SystemExit(message)
    return data


def trailer(env: Mapping[str, str]) -> str:
    """The posting agent's ``Generated-by`` line.

    Raises:
        SystemExit: The invoker isn't an agent session, or its value is invalid.
    """
    value = pr.posting_value(env)
    if value is None:
        message = "not closing: the caller isn't a recognized agent session"
        raise SystemExit(message)
    if why := agent_identity.check_value(value):
        message = f"invalid LLM trailer: {value}: {why}"
        raise SystemExit(message)
    return f"{agent_identity.KEY_GEN}: {value}"


def ref_of(url: str) -> str:
    """``owner/repo#N`` for a pull request URL."""
    found = pr.PR_URL.search(url)
    if found is None:
        message = f"cannot read a pull request from {url}"
        raise SystemExit(message)
    return f"{found[1]}#{found[2]}"


def opener_note(data: Mapping[str, object], env: Mapping[str, str]) -> str:
    """The comment line a close by a session other than the opener needs, or ''.

    Raises:
        SystemExit: The pull request has no ``Opened-by`` line, or the ledger
            can't be read.
    """
    if pr.opened_line(str(data.get("body") or "")) is None:  # pragma: no mutate
        message = "not closing: the pull request has no Opened-by line (a human's?)"
        raise SystemExit(message)
    ident = opened.opener(env)
    try:
        own = ident is not None and ref_of(str(data["url"])) in opened.read_ledger(
            ident.root
        )
    except opened.LedgerError as exc:
        raise SystemExit(str(exc)) from exc
    return "" if own else "Closed by a session other than the opener.\n\n"


def close_comment(reason: str, note: str, line: str) -> str:
    """The final comment: a note, the reason, then the trailer."""
    return f"{note}{reason}\n\n{line}\n"


def run_close(argv: list[str], *, dry_run: bool) -> int:
    """Run ``gh ... close``, or print what would run; the exit code."""
    if dry_run:
        print(shlex.join(["gh", *argv]))
        return 0
    done = gh(argv)
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    return done.returncode


def delete_branch(data: Mapping[str, object], *, dry_run: bool) -> int:
    """Delete the head branch of a closed pull request; the exit code.

    A branch on a fork is left alone, with a note.
    """
    branch = str(data["headRefName"])
    if data["isCrossRepository"]:
        print(f"not deleting branch {branch}: it is on a fork")
        return 0
    ref = urllib.parse.quote(branch)
    path = f"repos/{ref_of(str(data['url'])).split('#')[0]}/git/refs/heads/{ref}"
    words = ["api", "-X", "DELETE", path]
    if dry_run:
        print(f"would delete branch {branch}: {shlex.join(['gh', *words])}")
        return 0
    done = gh(words)
    print(done.stderr, end="", file=sys.stderr)
    return done.returncode


def check_dependents(data: Mapping[str, object]) -> None:
    """Refuse when open pull requests target the head branch about to go.

    A branch on a fork isn't deleted, so it isn't checked.

    Raises:
        SystemExit: Open pull requests target the branch, or gh can't say.
    """
    if data["isCrossRepository"]:
        return
    branch = str(data["headRefName"])
    repo = ref_of(str(data["url"])).split("#")[0]
    words = ["pr", "list", "--repo", repo, "--base", branch, "--state", "open"]
    done = gh([*words, "--json", "number"])
    found = pr.gh_data(done)
    numbers: list[int] = []
    if isinstance(found, list):
        items: list[object] = found
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("number"), int):
                # Any non-list value sends this to the refusal below, so
                # None and "" are equivalent here.
                found = None  # pragma: no mutate
                break
            numbers.append(item["number"])
    if not isinstance(found, list):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"not closing: cannot list open PRs targeting {branch}: {why}"
        raise SystemExit(message)
    if numbers:
        refs = ", ".join(f"#{number}" for number in sorted(numbers))
        message = f"not closing: open PRs target branch {branch}: {refs}"
        raise SystemExit(message)


def non_blank(text: str) -> str:
    """The text as given, unless it is empty or only whitespace.

    Raises:
        argparse.ArgumentTypeError: The text is blank.
    """
    if not text.strip():
        message = "must not be blank"
        raise argparse.ArgumentTypeError(message)
    return text


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(prog=prog, description="Close a PR or issue.")
    sub = parser.add_subparsers(dest="kind", required=True)
    for kind in ("pr", "issue"):
        one = sub.add_parser(kind).add_subparsers(dest="verb", required=True)
        closing = one.add_parser("close")
        closing.add_argument("number", type=int)
        closing.add_argument("--dry-run", action="store_true")
        if kind == "pr":
            closing.add_argument("--reason", required=True, type=non_blank)
            closing.add_argument("--delete-branch", action="store_true")
        else:
            closing.add_argument("--reason", required=True, choices=sorted(REASONS))
            closing.add_argument("--comment", type=non_blank)
    return parser


def main(
    argv: Sequence[str] | None = None,
    prog: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Close the pull request or issue the arguments name."""
    args = build_parser(prog).parse_args(argv)
    env = os.environ if env is None else env
    line = trailer(env)
    if args.kind == "pr":
        data = view("pr", args.number)
        text = close_comment(args.reason, opener_note(data, env), line)
        if args.delete_branch:
            check_dependents(data)
        words = ["pr", "close", str(args.number), "--comment", text]
    else:
        data = view("issue", args.number)
        text = close_comment(args.comment or "Closed.", "", line)
        reason = REASONS[args.reason]
        words = ["issue", "close", str(args.number), "--reason", reason]
        words += ["--comment", text]
    code = run_close(words, dry_run=args.dry_run)
    if args.kind == "pr" and args.delete_branch and code == 0:
        return delete_branch(data, dry_run=args.dry_run)
    return code
