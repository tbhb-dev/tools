# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""``tbhbdev alpha pr-stats``: cycle time of merged and closed pull requests.

A pull request's cycle time starts at the earliest of every ``started_at`` on
its identity lines and the committer date of its earliest commit. It ends at
``mergedAt``, or at ``closedAt`` for a pull request closed without merging,
which the row records as its own state. Each row names the source that set the
start, ``started_at`` or ``commit``.
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha import identity
from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FIELDS = "number,state,createdAt,mergedAt,closedAt,body,commits"
START_ATTR = re.compile(r"(?<![^\s;])started_at=([^\s;]+)")


def parse_time(value: object) -> datetime | None:
    """A UTC time as GitHub and the trailer write it, or None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def line_starts(text: object) -> tuple[list[datetime], int]:
    """The valid ``started_at`` times in a text, and its identity lines without one."""
    starts: list[datetime] = []
    missing = 0
    for _key, value in identity.llm_trailers(text if isinstance(text, str) else ""):
        found = START_ATTR.search(value)
        when = parse_time(found[1]) if found else None
        if when is None:
            missing += 1
        else:
            starts.append(when)
    return starts, missing


def row(repo: str, data: Mapping[str, object]) -> dict[str, object] | None:
    """One stats row, or None for an open pull request or one with no start."""
    merged = parse_time(data.get("mergedAt"))
    end = merged or parse_time(data.get("closedAt"))
    commits = cast("list[Mapping[str, object]]", data.get("commits") or [])
    starts, missing = line_starts(data.get("body"))
    for commit in commits:
        found, lacking = line_starts(commit.get("messageBody"))
        starts += found
        missing += lacking
    committed = [t for c in commits if (t := parse_time(c.get("committedDate")))]
    first_line = min(starts, default=None)
    first_commit = min(committed, default=None)
    if end is None or (first_line is None and first_commit is None):
        return None
    if first_line is not None and (first_commit is None or first_line <= first_commit):
        start, source = first_line, "started_at"
    else:
        start, source = cast("datetime", first_commit), "commit"
    return {
        "repo": repo,
        "number": data.get("number"),
        "state": "merged" if merged else "closed",
        "start": start.strftime(TIME_FORMAT),
        "start_source": source,
        "end": end.strftime(TIME_FORMAT),
        "cycle_seconds": int((end - start).total_seconds()),
        "lines_without_started_at": missing,
    }


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    """Print one JSON row per merged or closed pull request of a repository."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("repo", help="OWNER/REPO")
    parser.add_argument("--limit", type=int, default=200, help="pull requests to read")
    args = parser.parse_args(argv)
    listed = pr.gh_json(
        [
            "pr",
            "list",
            "--repo",
            args.repo,
            "--state",
            "all",
            "--limit",
            str(args.limit),
            "--json",
            FIELDS,
        ]
    )
    for data in listed:
        found = row(args.repo, data)
        if found is None and data.get("state") != "OPEN":
            print(f"{prog}: no start for #{data.get('number')}", file=sys.stderr)
        elif found is not None:
            print(json.dumps(found))
    return 0
