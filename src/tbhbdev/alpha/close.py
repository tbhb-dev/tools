# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""``tbhbdev alpha pr close`` and ``tbhbdev alpha issue close``.

Both close through the agent's own ``gh``, as the agent's GitHub account, and
leave a final comment that holds the reason and the posting agent's
``Generated-by`` trailer. Each refuses anything that isn't open, and an
invoker that isn't a recognised agent session, because the trailer names one.

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
from typing import TYPE_CHECKING

from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import opened
from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Mapping, Sequence

REASONS = {"completed": "completed", "not-planned": "not planned"}


def gh(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run gh with the arguments."""
    return pr.run([pr.find_tool(pr.GH), *argv])


def view(kind: str, number: int) -> dict[str, object]:
    """The ``state``, ``url`` and ``body`` of a pull request or an issue.

    Raises:
        SystemExit: gh can't read it, or it isn't open.
    """
    done = gh([kind, "view", str(number), "--json", "state,url,body"])
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
        message = "not closing: the caller isn't a recognised agent session"
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
            closing.add_argument("--reason", required=True)
        else:
            closing.add_argument("--reason", required=True, choices=sorted(REASONS))
            closing.add_argument("--comment")
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
        words = ["pr", "close", str(args.number), "--comment", text]
    else:
        view("issue", args.number)
        text = close_comment(args.comment or "Closed.", "", line)
        reason = REASONS[args.reason]
        words = ["issue", "close", str(args.number), "--reason", reason]
        words += ["--comment", text]
    return run_close(words, dry_run=args.dry_run)
