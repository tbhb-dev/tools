"""Draft architecture decision records from GitHub issues."""

# cspell:words adrs

import fcntl
import json
import os
import re
import sys
import unicodedata
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from datetime import date
from functools import partial
from pathlib import Path
from subprocess import CalledProcessError  # nosec B404 -- Only the client error type.
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from tbhbdev import runner
from tbhbdev.adr_decision import Decision
from tbhbdev.alpha.pull_request import origin_repository, parse_frontmatter
from tbhbdev.alpha.repo_bootstrap import repo_name
from tbhbdev.content import Client, GhClient, Refused, check_decision_record

if TYPE_CHECKING:
    from collections.abc import Callable

RECORD_PATTERN = "[0-9][0-9][0-9][0-9]-*.md"
LAST_NUMBER = 9999
FRONTMATTER = re.compile(r"\A---(\r?\n)(.*?)^---[ \t]*\r?$", re.DOTALL | re.MULTILINE)


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        message = "expected a positive number"
        raise ArgumentTypeError(message)
    return number


def _deprecated(text: str, number: int) -> str:
    match = FRONTMATTER.match(text)
    before, _ = parse_frontmatter(text.replace("\r\n", "\n"))
    message = "expected block YAML frontmatter on the replaced record"
    if match is None or not isinstance(before, dict):
        raise Refused(message)
    newline, source = match.groups()
    changes = {"status": "deprecated", "superseded_by": number}
    for key, value in changes.items():
        pattern = rf"^({key}:[ \t]+)[^#\r\n]+?([ \t]*(?:#[^\r\n]*)?)(?=\r?$)"
        source, count = re.subn(
            pattern, rf"\g<1>{value}\g<2>", source, flags=re.MULTILINE
        )
        if not count:
            source += f"{key}: {value}{newline}"
    updated = text[: match.start(2)] + source + text[match.end(2) :]
    after, _ = parse_frontmatter(updated.replace("\r\n", "\n"))
    if after != before | changes:
        raise Refused(message)
    return updated


def _replace(path: Path, text: str) -> None:
    with TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        temporary.write_bytes(text.encode())
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)


def _write(
    directory: Path, supersedes: int | None, decision: Decision, today: date
) -> tuple[Path, str]:
    folded = unicodedata.normalize("NFKD", decision.title).encode("ascii", "ignore")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.decode().lower()).strip("-")
    if not slug:
        message = "issue title needs an ASCII letter or digit for the filename"
        raise Refused(message)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _write_next(directory, slug, supersedes, decision, today)
    finally:
        os.close(descriptor)


def _write_next(
    directory: Path, slug: str, supersedes: int | None, decision: Decision, today: date
) -> tuple[Path, str]:
    records = sorted(path for path in directory.glob(RECORD_PATTERN) if path.is_file())
    number = int(records[-1].name[:4]) + 1 if records else 1
    if number > LAST_NUMBER:
        message = "no four-digit decision record numbers remain"
        raise Refused(message)

    context = decision.question
    update = None
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
        original = replaced.read_bytes().decode()
        update = replaced, _deprecated(original, number)
        context += f"\n\nThis record supersedes [{supersedes:04d}]({replaced.name})."
    title = f"{number:04d}. {decision.title}"
    body = (
        f"# {title}\n\n## Context\n\n{context}\n\n## Decision\n\n{decision.choice}\n\n"
        f"## Consequences\n\n{decision.undo}\n"
    )
    frontmatter = (
        f"---\ntype: Decision Record\ntitle: {json.dumps(title, ensure_ascii=False)}\n"
        f"description: {json.dumps(decision.question, ensure_ascii=False)}\n"
        f'tags: [adrs, decisions]\nstatus: stable\ndecided: "{today.isoformat()}"\n'
        "---\n\n"
    )
    path = directory / f"{number:04d}-{slug}.md"
    with path.open("xb") as stream:
        try:
            stream.write((frontmatter + body).encode())
            stream.close()
            if update is not None:
                _replace(*update)
        except OSError:
            path.unlink()
            raise
    return path, body


def _new(args: Namespace, client: Client | None, clock: Callable[[], date]) -> int:
    api = GhClient() if client is None else client
    try:
        repo = args.repo or origin_repository()
        issue = api.rest("GET", f"/repos/{repo}/issues/{args.from_decision}")
        decision = Decision.parse(issue)
        path, body = _write(args.dir, args.supersedes, decision, clock())
    except SystemExit as exc:
        message = f"{exc}. Supply --repo OWNER/REPO"
        raise Refused(message) from exc
    except CalledProcessError as exc:
        message = "cannot read Decision issue from GitHub"
        raise Refused(message) from exc
    except FileExistsError as exc:
        message = f"record already exists: {exc.filename}"
        raise Refused(message) from exc
    except (OSError, ValueError) as exc:
        raise Refused(str(exc)) from exc
    for finding in check_decision_record(body):
        print(f"Draft needs editing. {finding}", file=sys.stderr)
    print(path)
    return 0


def main(
    argv: list[str],
    prog: str,
    *,
    client: Client | None = None,
    clock: Callable[[], date] = date.today,
) -> int:
    parser = ArgumentParser(prog=prog)
    new = parser.add_subparsers(required=True).add_parser("new")
    new.add_argument("--from-decision", type=_positive, required=True, metavar="N")
    new.add_argument("--repo", type=repo_name, metavar="OWNER/REPO")
    new.add_argument("--dir", type=Path, default=Path("adrs"), metavar="DIR")
    new.add_argument("--supersedes", type=_positive, metavar="NNNN")
    new.set_defaults(run=partial(_new, client=client, clock=clock))
    return runner.main(parser, argv, Refused)
