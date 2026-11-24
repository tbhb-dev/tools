"""Draft an architecture decision record from a Decision issue."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import unicodedata
from datetime import date
from functools import partial
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING
from urllib.parse import quote

import yaml

from tbhbdev import runner
from tbhbdev.adr_decision import Decision
from tbhbdev.alpha.pull_request import NAME_WITH_OWNER, origin_repository
from tbhbdev.content import GhClient, IssueRef, Refused, check_decision_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from tbhbdev.content import Client

_RECORD_PATTERN = "[0-9][0-9][0-9][0-9]-*.md"
_LAST_NUMBER = 9999
_FRONTMATTER = re.compile(
    r"\A---(?P<newline>\r?\n)(?P<yaml>.*?)^---[ \t]*\r?$", re.DOTALL | re.MULTILINE
)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        message = "expected a positive number"
        raise argparse.ArgumentTypeError(message)
    return number


def _repository(value: str) -> str:
    if NAME_WITH_OWNER.fullmatch(value) is None:
        message = "expected OWNER/REPO"
        raise argparse.ArgumentTypeError(message)
    return value


def _slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore")
    return re.sub(r"[^a-z0-9]+", "-", ascii_title.decode().lower()).strip("-")


def _read_issue(repo: str | None, number: int, client: Client) -> Decision:
    if repo is None:
        try:
            repo = origin_repository()
        except SystemExit as exc:
            message = f"{exc}. Supply --repo OWNER/REPO"
            raise Refused(message) from exc
    owner, name = repo.split("/", 1)
    try:
        issue = client.rest("GET", IssueRef(owner, name, number).path)
    except (CalledProcessError, OSError, ValueError) as exc:
        message = f"cannot read issue {repo}#{number} from GitHub"
        raise Refused(message) from exc
    return Decision.parse(issue)


def _deprecated(path: Path, number: int) -> str:
    text = path.read_text(encoding="utf-8", newline="")
    match = _FRONTMATTER.match(text)
    message = f"expected block YAML frontmatter in {path}"
    if match is None:
        raise Refused(message)
    source = match["yaml"]
    try:
        node = yaml.compose(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise Refused(message) from exc
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        raise Refused(message)

    values = {str(key.value): value for key, value in node.value}
    changes = []
    for key, replacement in {
        "status": "deprecated",
        "superseded_by": str(number),
    }.items():
        value = values.get(key)
        if value is None:
            changes.append(
                (len(source), len(source), f"{key}: {replacement}{match['newline']}")
            )
        elif (
            isinstance(value, yaml.ScalarNode)
            and value.style not in ("|", ">")
            and value.value
        ):
            changes.append((value.start_mark.index, value.end_mark.index, replacement))
        else:
            raise Refused(message)
    for start, end, replacement in sorted(changes, reverse=True):
        source = source[:start] + replacement + source[end:]
    return text[: match.start("yaml")] + source + text[match.end("yaml") :]


def _replace(path: Path, text: str) -> None:
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            temporary.chmod(path.stat().st_mode & 0o777)
            handle.write(text)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _render(
    decision: Decision, number: int, today: date, replaced: Path | None
) -> tuple[str, str]:
    title = f"{number:04d}. {decision.title}"
    frontmatter = (
        f"---\ntype: Decision Record\ntitle: {json.dumps(title, ensure_ascii=False)}\n"
        f"description: {json.dumps(decision.question, ensure_ascii=False)}\n"
        f'tags: [adrs, decisions]\nstatus: stable\ndecided: "{today.isoformat()}"\n'
        "---\n\n"
    )
    context = decision.question
    if replaced is not None:
        context += (
            f"\n\nThis record supersedes [{replaced.name[:4]}]({quote(replaced.name)})."
        )
    body = (
        f"# {title}\n\n## Context\n\n{context}\n\n## Decision\n\n{decision.choice}\n\n"
        f"## Consequences\n\n{decision.undo}\n"
    )
    return frontmatter, body


def _write_record(
    args: argparse.Namespace, decision: Decision, today: date
) -> tuple[Path, list[str]]:
    slug = _slug(decision.title)
    if not slug:
        message = "issue title needs a letter or digit for the record filename"
        raise Refused(message)
    directory: Path = args.dir
    # Lock the directory so two commands with different titles pick different numbers.
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _write_next(directory, slug, args.supersedes, decision, today)
    finally:
        os.close(descriptor)


def _write_next(
    directory: Path, slug: str, supersedes: int | None, decision: Decision, today: date
) -> tuple[Path, list[str]]:
    records = sorted(path for path in directory.glob(_RECORD_PATTERN) if path.is_file())
    number = int(records[-1].name[:4]) + 1 if records else 1
    if number > _LAST_NUMBER:
        message = "no four-digit decision record numbers remain"
        raise Refused(message)

    replaced = None
    if supersedes is not None:
        matches = [
            path for path in records if path.name.startswith(f"{supersedes:04d}-")
        ]
        if len(matches) != 1:
            message = (
                f"expected one record numbered {supersedes:04d}, found {len(matches)}"
            )
            raise Refused(message)
        replaced = matches[0]
        deprecated = _deprecated(replaced, number)

    path = directory / f"{number:04d}-{slug}.md"
    frontmatter, body = _render(decision, number, today, replaced)
    findings = check_decision_record(body)
    try:
        stream = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        message = f"record already exists: {path}"
        raise Refused(message) from exc
    try:
        with stream:
            stream.write(frontmatter + body)
        if replaced is not None:
            _replace(replaced, deprecated)
    except OSError:
        path.unlink()
        raise
    return path, findings


def _new(
    args: argparse.Namespace,
    *,
    client: Client | None,
    clock: Callable[[], date],
    prog: str,
) -> int:
    decision = _read_issue(
        args.repo, args.from_decision, GhClient() if client is None else client
    )
    try:
        path, findings = _write_record(args, decision, clock())
    except OSError as exc:
        message = f"cannot write decision record: {exc}"
        raise Refused(message) from exc
    for finding in findings:
        print(f"{prog}: draft needs editing. {finding}", file=sys.stderr)
    print(path)
    return 0


def main(
    argv: list[str],
    prog: str,
    *,
    client: Client | None = None,
    clock: Callable[[], date] = date.today,
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog, description="Work with decision records."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="Draft a decision record from an issue.")
    new.add_argument("--from-decision", type=_positive, required=True, metavar="N")
    new.add_argument("--repo", type=_repository, metavar="OWNER/REPO")
    new.add_argument("--dir", type=Path, default=Path("adrs"), metavar="DIR")
    new.add_argument("--supersedes", type=_positive, metavar="NNNN")
    new.set_defaults(run=partial(_new, client=client, clock=clock, prog=prog))
    return runner.main(parser, argv, Refused)
