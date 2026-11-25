# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""``tbhbdev alpha pr close`` and ``tbhbdev alpha issue`` commands.

The close commands use the agent's own ``gh`` and GitHub account, and
leave a final comment that holds the reason and the posting agent's
``Generated-by`` trailer. Each refuses anything that isn't open, and an
invoker that isn't a recognized agent session, because the trailer names one.

Provisional, until @tbhb decides open question 11 of the alpha-review
proposal: a session closes a pull request it opened freely, which the opened
ledger decides. It closes another agent's pull request only with a reason,
which the comment records along with the fact that the closer isn't the opener.
A pull request with no ``Opened-by`` line, such as one opened before the line
existed, is closed only when its GitHub author is the account this ``gh`` runs
as or a bot account, such as Renovate's, and the comment records that basis. A
human's is refused. Any session may
close an issue with a reason.
"""

import argparse
import json
import os
import re
import shlex
import subprocess  # nosec B404
import sys
import urllib.parse
from pathlib import Path
from textwrap import indent
from typing import TYPE_CHECKING

from tbhbdev import content, issue_import
from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import issue_body, opened
from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha.issue_body import ISSUE_TYPES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REASONS = {"completed": "completed", "not-planned": "not planned"}
RELATIONSHIP_WRITES = {
    "link-sub": content.link_sub_issue,
    "unlink-sub": content.unlink_sub_issue,
    "block": content.link_dependency,
    "unblock": content.unlink_dependency,
}


def gh(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run gh with the arguments."""
    return pr.run([pr.find_tool(pr.GH), *argv])


def view(kind: str, number: int, repo: str | None = None) -> dict[str, object]:
    """The ``state``, ``url`` and ``body`` of a pull request or an issue.

    A pull request also reports ``author``, ``headRefName`` and
    ``isCrossRepository``.

    Raises:
        SystemExit: gh can't read it, or it isn't open.
    """
    fields = "state,url,body"
    if kind == "pr":
        fields += ",author,headRefName,isCrossRepository"
    pin = [] if repo is None else ["--repo", f"github.com/{repo}"]
    done = gh([kind, "view", str(number), *pin, "--json", fields])
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


def agent_author(data: Mapping[str, object]) -> str | None:
    """The pull request's author login when it is this ``gh``'s own account."""
    author = data.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(login, str) or not login:
        return None
    me = gh(["api", "--hostname", "github.com", "user", "--jq", ".login"])
    return login if me.returncode == 0 and me.stdout.strip() == login else None


def bot_author(data: Mapping[str, object]) -> str | None:
    """The pull request's author login when GitHub marks the author a bot."""
    author = data.get("author")
    if not isinstance(author, dict) or author.get("is_bot") is not True:
        return None
    login = author.get("login")
    return login if isinstance(login, str) and login else None


def opener_note(data: Mapping[str, object], env: Mapping[str, str]) -> str:
    """The comment line a close by a session other than the opener needs, or ''.

    A pull request with no ``Opened-by`` line takes the author basis instead.

    Raises:
        SystemExit: The pull request has no ``Opened-by`` line and isn't the
            agent account's, or the ledger can't be read.
    """
    if pr.opened_line(str(data.get("body") or "")) is None:  # pragma: no mutate
        login = agent_author(data)
        if login is not None:
            return (
                "Closed on the basis that its GitHub author is the agent account"
                f" {login} and it has no Opened-by line.\n\n"
            )
        login = bot_author(data)
        if login is None:
            message = "not closing: the pull request has no Opened-by line (a human's?)"
            raise SystemExit(message)
        return (
            f"Closed on the basis that its GitHub author is the bot account {login}"
            " and it has no Opened-by line.\n\n"
        )
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


def issue_reference(text: str) -> tuple[str, int]:
    """Parse a qualified or local issue reference."""
    repo, _, number = text.partition("#")
    if not re.fullmatch(r"[1-9][0-9]*", number) or (
        repo and not pr.NAME_WITH_OWNER.fullmatch(repo)
    ):
        message = "expected owner/repo#N or #N"
        raise argparse.ArgumentTypeError(message)
    return repo, int(number)


def issue_repository(text: str) -> str:
    if not pr.NAME_WITH_OWNER.fullmatch(text):
        message = "expected owner/repo"
        raise argparse.ArgumentTypeError(message)
    return text


def parent_reference(text: str) -> tuple[str, int]:
    if text.startswith("#"):
        message = "expected N or owner/repo#N"
        raise argparse.ArgumentTypeError(message)
    return issue_reference(text if "#" in text else f"#{text}")


def validate_issue(args: argparse.Namespace, body: str) -> None:
    if refusals := issue_body.check(body, args.type, args.pm_dir):
        raise content.Refused("\n".join(refusals))
    if args.pm_dir is not None:
        return
    if args.type not in issue_import.TYPES:
        print(f"no schema check for type {args.type}", file=sys.stderr)
        return
    title: str = " ".join(args.title.splitlines())
    # The parent lives on GitHub. The importer can only resolve local blocks.
    parent_field = ""
    unresolved = set()
    if args.parent is not None:
        repo, number = args.parent
        parent_field = f"parent: {repo or args.repo}#{number}\n"
        unresolved.add("unknown parent target")
        if required := issue_import.PARENT_TYPES.get(args.type):
            unresolved.add(f"{args.type} requires a parent of type {required}")
    # Indentation keeps headings in evidence from starting another block.
    source = (
        f"### issue\ntype: {args.type}\ntitle: {title}\n"
        f"{parent_field}{indent(body, ' ')}"
    )
    blocks, findings = issue_import.read(source)
    findings.extend(f for f in issue_import.check(blocks) if f.rule not in unresolved)
    if findings:
        message = "\n".join(f"Refused {finding}" for finding in findings)
        raise content.Refused(message)


def write_issue(args: argparse.Namespace) -> int:
    caller = "tbhbdev alpha issue create"
    if args.reason is not None:
        caller += f": {args.reason}"
    try:
        body = args.body_file.read_bytes().decode("utf-8")
        validate_issue(args, body)
        result = content.create_issue(
            args.repo,
            args.title,
            body,
            issue_type=args.type,
            labels=args.label,
            caller=caller,
            dry_run=args.dry_run,
        )
        if not isinstance(result, content.IssueRef):
            print(json.dumps(result))
            return 0
        print(f"https://github.com/{result.owner}/{result.repo}/issues/{result.number}")
        print(result.number, flush=True)
        if args.parent is not None:
            repo, number = args.parent
            owner, name = (repo or args.repo).split("/")
            content.link_sub_issue(
                content.IssueRef(owner, name, number), result, caller=caller
            )
    except (OSError, UnicodeError, content.Refused) as exc:
        print(exc, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, end="", file=sys.stderr)
        return exc.returncode
    return 0


def add_creation_arguments(creating: argparse.ArgumentParser) -> None:
    creating.add_argument(
        "--repo", required=True, type=issue_repository, metavar="OWNER/REPO"
    )
    creating.add_argument("--type", required=True, choices=sorted(ISSUE_TYPES))
    creating.add_argument("--title", required=True)
    creating.add_argument("--body-file", required=True, type=Path, metavar="FILE")
    creating.add_argument("--pm-dir", type=Path, metavar="DIR")
    creating.add_argument("--label", action="append", default=[])
    creating.add_argument("--parent", type=parent_reference, metavar="N|OWNER/REPO#N")
    creating.add_argument("--reason", type=non_blank)
    creating.add_argument("--dry-run", action="store_true")


def write_relationship(args: argparse.Namespace) -> int:
    """Resolve both references and send the relationship write to content."""
    if args.blocking is not None:
        first, second = args.blocking, args.first
    else:
        first, second = args.first, args.second
    first_repo, first_number = first
    second_repo, second_number = second
    if not first_repo or not second_repo:
        repo = pr.origin_repository()
        first_repo = first_repo or repo
        second_repo = second_repo or repo
    first_owner, first_name = first_repo.split("/")
    second_owner, second_name = second_repo.split("/")
    try:
        result = RELATIONSHIP_WRITES[args.verb](
            content.IssueRef(first_owner, first_name, first_number),
            content.IssueRef(second_owner, second_name, second_number),
            caller="tbhbdev alpha issue",
            dry_run=args.dry_run,
        )
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, end="", file=sys.stderr)
        return exc.returncode
    except content.Refused as exc:
        print(exc, file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result))
    return 0


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog=prog, description="Close a PR or manage issues."
    )
    sub = parser.add_subparsers(dest="kind", required=True)
    for kind in ("pr", "issue"):
        one = sub.add_parser(kind).add_subparsers(dest="verb", required=True)
        closing = one.add_parser("close")
        closing.add_argument("number", type=int)
        closing.add_argument("--dry-run", action="store_true")
        if kind == "pr":
            closing.add_argument("--reason", "--comment", required=True, type=non_blank)
            closing.add_argument("--delete-branch", action="store_true")
        else:
            closing.add_argument("--reason", required=True, choices=sorted(REASONS))
            closing.add_argument("--comment", type=non_blank)
            add_creation_arguments(one.add_parser("create"))
            for verb in RELATIONSHIP_WRITES:
                relationship = one.add_parser(verb)
                relationship.add_argument("--dry-run", action="store_true")
                if verb in {"block", "unblock"}:
                    relationship.add_argument(
                        "first", metavar="ISSUE", type=issue_reference
                    )
                    direction = relationship.add_mutually_exclusive_group(required=True)
                    direction.add_argument(
                        "--by",
                        "--blocked-by",
                        dest="second",
                        metavar="BLOCKER",
                        type=issue_reference,
                    )
                    direction.add_argument(
                        "--blocking", metavar="BLOCKED", type=issue_reference
                    )
                else:
                    relationship.add_argument(
                        "first", metavar="PARENT", type=issue_reference
                    )
                    relationship.add_argument(
                        "second", metavar="CHILD", type=issue_reference
                    )
                    relationship.set_defaults(blocking=None)
    return parser


def main(
    argv: Sequence[str] | None = None,
    prog: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run the close or issue write the arguments name."""
    args = build_parser(prog).parse_args(argv)
    if args.verb == "create":
        trailer(os.environ if env is None else env)
        return write_issue(args)
    if args.verb != "close":
        return write_relationship(args)
    env = os.environ if env is None else env
    line = trailer(env)
    repo = pr.origin_repository()
    if args.kind == "pr":
        data = view("pr", args.number, repo)
        text = close_comment(args.reason, opener_note(data, env), line)
        if args.delete_branch:
            check_dependents(data)
        words = ["pr", "close", str(args.number), "--repo", f"github.com/{repo}"]
        words += ["--comment", text]
    else:
        data = view("issue", args.number, repo)
        text = close_comment(args.comment or "Closed.", "", line)
        reason = REASONS[args.reason]
        words = ["issue", "close", str(args.number), "--repo", f"github.com/{repo}"]
        words += ["--reason", reason]
        words += ["--comment", text]
    code = run_close(words, dry_run=args.dry_run)
    if args.kind == "pr" and args.delete_branch and code == 0:
        return delete_branch(data, dry_run=args.dry_run)
    return code
