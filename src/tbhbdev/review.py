"""Classify a pull request and post its review-tier commit status."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypedDict, cast

import httpx

from tbhbdev import userconfig
from tbhbdev.alpha import gh_app, repo
from tbhbdev.alpha.agent import credentials, review_post, settings
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.guard_gh_pr import repo_name

LOCAL_CONFIG = ".github/review-tiers.local.toml"
PROSE_REPOS = {"wiki", "drafts", "research", "spikes"}
PROSE_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
PAGE_SIZE = 100
FILE_LIMIT = 3000
PATCH_LINE = re.compile(r"^([+-])(.*)$", re.MULTILINE)
CONFIG_ENV = "TBHBDEV_REVIEW_TIERS"
LOWER_TIER = "a local rule would lower a review tier"


@dataclass(frozen=True)
class Change:
    paths: tuple[PurePosixPath, ...]
    lines: tuple[str, ...]
    removed: bool
    incomplete: bool


class File(TypedDict):
    filename: str
    status: str
    additions: int
    deletions: int
    patch: NotRequired[str]
    previous_filename: NotRequired[str]


@dataclass(frozen=True)
class Rule:
    tier: int
    selector: str | re.Pattern[str]
    reason: str

    def matches(self, change: Change) -> bool:
        if isinstance(self.selector, str):
            return any(path.full_match(self.selector) for path in change.paths)
        return any(map(self.selector.search, change.lines))


def load_rules(text: str, source: str) -> list[Rule]:
    """Load the temporary tier table schema from either policy file."""
    rules: list[Rule] = []
    try:
        document = tomllib.loads(text)
        tiers: object = document.get("tier", {})
        if document.keys() - {"tier"} or not isinstance(tiers, dict):
            raise ValueError  # noqa: TRY301
        for tier, table in cast("dict[str, object]", tiers).items():
            if tier not in {"0", "1", "2"} or not isinstance(table, dict):
                raise ValueError  # noqa: TRY301
            for key, entries in cast("dict[str, object]", table).items():
                rules.extend(read_entries(int(tier), source, key, entries))
    except ValueError, re.PatternError:
        message = f"invalid {source} review tier config"
        raise Invalid(message) from None
    return rules


def read_entries(tier: int, source: str, key: str, entries: object) -> list[Rule]:
    if key not in {"paths", "patterns"} or not isinstance(entries, list):
        raise ValueError
    rules: list[Rule] = []
    for index, entry in enumerate(cast("list[object]", entries), 1):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError
        selector = re.compile(entry) if key == "patterns" else entry
        label = key.removesuffix("s")
        rules.append(Rule(tier, selector, f"{source} {label} rule {index}"))
    return rules


def parse_change(row: File) -> Change:
    path = row["filename"]
    paths = dict.fromkeys((path, row.get("previous_filename", path)))
    lines = PATCH_LINE.findall(row.get("patch", ""))
    added = sum(mark == "+" for mark, _ in lines)
    return Change(
        tuple(map(PurePosixPath, paths)),
        tuple(line for _, line in lines),
        row["status"] == "removed",
        (added, len(lines) - added) != (row["additions"], row["deletions"]),
    )


def baseline(change: Change, name: str) -> tuple[int, str]:
    if change.removed:
        return 2, "file deletion"
    if change.incomplete:
        return 2, "incomplete text diff"
    if PurePosixPath(LOCAL_CONFIG) in change.paths:
        return 2, "local policy change"
    if name.lower() in PROSE_REPOS and all(
        not path.is_relative_to("designs") and path.suffix.lower() in PROSE_SUFFIXES
        for path in change.paths
    ):
        return 0, "prose-only change"
    return 1, "default review"


def classify(
    name: str, changes: list[Change], global_rules: list[Rule], local_rules: list[Rule]
) -> tuple[int, list[str]]:
    tier_two = {rule.selector for rule in global_rules if rule.tier > 1}
    if any(
        rule.tier == 0 or (rule.tier == 1 and rule.selector in tier_two)
        for rule in local_rules
    ):
        raise Invalid(LOWER_TIER)
    matches: list[tuple[int, str]] = []
    for change in changes:
        matches.extend(change_reasons(change, name, global_rules, local_rules))
    tier = max((level for level, _ in matches), default=1)
    reasons = dict.fromkeys(reason for level, reason in matches if level == tier)
    return tier, list(reasons) or ["empty diff"]


def change_reasons(
    change: Change, name: str, global_rules: list[Rule], local_rules: list[Rule]
) -> list[tuple[int, str]]:
    found = [baseline(change, name)]
    found.extend(
        (rule.tier, rule.reason) for rule in global_rules if rule.matches(change)
    )
    floor = max(tier for tier, _ in found)
    additions = [
        (rule.tier, rule.reason) for rule in local_rules if rule.matches(change)
    ]
    if any(tier < floor for tier, _ in additions):
        raise Invalid(LOWER_TIER)
    return found + additions


def snapshot(client: httpx.Client, pull: str) -> tuple[str, str, int]:
    meta = gh_app.json_object(client.get(pull).raise_for_status())
    head = review_post.pull_head(meta)
    base = review_post.pull_head({"head": meta.get("base")})
    count = meta.get("changed_files")
    if head and base and isinstance(count, int) and 0 <= count <= FILE_LIMIT:
        return head, base, count
    message = "GitHub returned invalid pull request metadata or too many files"
    raise Invalid(message)


def diff(client: httpx.Client, pull: str, count: int) -> list[Change]:
    changes: list[Change] = []
    for page, start in enumerate(range(0, count, PAGE_SIZE), 1):
        params = {"per_page": PAGE_SIZE, "page": page}
        rows: object = (
            client.get(f"{pull}/files", params=params).raise_for_status().json()
        )
        if not isinstance(rows, list) or len(rows) != min(PAGE_SIZE, count - start):
            message = "GitHub returned an incomplete file list"
            raise Invalid(message)
        changes.extend(map(parse_change, cast("list[File]", rows)))
    return changes


def post(
    client: httpx.Client, repository: str, number: int, global_rules: list[Rule]
) -> tuple[int, list[str]]:
    root = f"/repos/{repository}"
    pull = f"{root}/pulls/{number}"
    head, base, count = snapshot(client, pull)
    changes = diff(client, pull, count)
    local = client.get(
        f"{root}/contents/{LOCAL_CONFIG}",
        params={"ref": base},
        headers={"Accept": "application/vnd.github.raw+json"},
    )
    text = (
        ""
        if local.status_code == httpx.codes.NOT_FOUND
        else local.raise_for_status().text
    )
    rules = load_rules(text, "local")
    tier, reasons = classify(repository.split("/")[1], changes, global_rules, rules)
    if snapshot(client, pull) != (head, base, count):
        message = "the pull request changed since its diff was read"
        raise Invalid(message)
    description = f"tier {tier}: {reasons[0]}"
    status = {"context": "review-tier", "state": "success", "description": description}
    client.post(f"{root}/statuses/{head}", json=status).raise_for_status()
    return tier, reasons


def repository_slug() -> str:
    code, url = repo.git("remote", "get-url", "origin")
    value = repo_name(url.replace(":", "/"))
    if code or not repo.is_tbhb_dev_url(url) or value is None:
        message = "run in a repository with a tbhb-dev GitHub origin"
        raise Invalid(message)
    return value


def app_token(repository: str, account: str | None) -> str:
    try:
        available = [
            refs
            for refs in settings.load().credentials
            if refs.pull_requests_app_client_id is not None
            and (account is None or refs.account == account)
        ]
        if len(available) == 1:
            return credentials.pull_requests_token(
                available[0],
                repository,
                {"contents": "read", "pull_requests": "read", "statuses": "write"},
            )
    except (*credentials.TOKEN_ERRORS, userconfig.ConfigError):
        message = "cannot mint a pull-requests App token"
        raise Invalid(message) from None
    message = "select one configured pull-requests App with --account"
    raise Invalid(message)


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    tier_parser = parser.add_subparsers(required=True).add_parser(
        "tier", help="post the review tier of a pull request"
    )
    tier_parser.add_argument("number", type=int)
    tier_parser.add_argument("--config", default=os.environ.get(CONFIG_ENV), type=Path)
    tier_parser.add_argument("--account")
    args = parser.parse_args(argv)
    try:
        if args.config is None or args.number < 1:
            message = f"a positive PR number and --config or {CONFIG_ENV} are required"
            raise Invalid(message)  # noqa: TRY301
        rules = load_rules(args.config.read_text(encoding="utf-8"), "global")
        repository = repository_slug()
        token = app_token(repository, args.account)
        with httpx.Client(
            base_url=gh_app.API,
            headers={"Authorization": f"Bearer {token}"},
            timeout=gh_app.TIMEOUT,
        ) as client:
            tier, reasons = post(client, repository, args.number, rules)
    except Invalid as exc:
        message = str(exc)
    except httpx.HTTPStatusError as exc:
        message = f"GitHub returned HTTP {exc.response.status_code}"
    except httpx.HTTPError, OSError, ValueError, KeyError, TypeError:
        message = "cannot read the policy or pull request"
    else:
        print(f"tier {tier}: {', '.join(reasons)}")
        return 0
    print(f"{prog}: {message}", file=sys.stderr)
    return 1
