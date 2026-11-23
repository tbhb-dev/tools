"""Classify a pull request and post the result on the reviewed commit."""

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

LOCAL_POLICY = ".github/review-tiers.local.toml"
POLICY_ENV = "TBHBDEV_REVIEW_TIERS"
PROSE_REPOS = {"wiki", "drafts", "research", "spikes"}
PROSE_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc"}
PAGE_SIZE = 100
FILE_LIMIT = 3000
SENSITIVE_TIER = 2
SHA = re.compile(r"[0-9a-f]{40}")
PATCH_LINES = re.compile(r"^([+-])(.*)$", re.MULTILINE)
LOWER_TIER = "a local rule would lower a review tier"


class ChangedFile(TypedDict):
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

    def matches(self, paths: tuple[PurePosixPath, ...], lines: list[str]) -> bool:
        selector = self.selector
        if isinstance(selector, str):
            return any(path.full_match(selector) for path in paths)
        return any(map(selector.search, lines))


def load_rules(text: str, source: str) -> list[Rule]:
    """Read the temporary policy schema used by both policy files."""
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
                rules.extend(read_selectors(int(tier), source, key, entries))
    except ValueError, re.PatternError:
        message = f"invalid {source} review tier policy"
        raise Invalid(message) from None
    return rules


def read_selectors(tier: int, source: str, key: str, entries: object) -> list[Rule]:
    if key not in {"paths", "patterns"} or not isinstance(entries, list):
        raise ValueError
    rules: list[Rule] = []
    for index, entry in enumerate(cast("list[object]", entries), 1):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError
        selector = re.compile(entry) if key == "patterns" else entry
        reason = f"{source} {key.removesuffix('s')} rule {index}"
        rules.append(Rule(tier, selector, reason))
    return rules


def baseline(
    name: str,
    row: ChangedFile,
    paths: tuple[PurePosixPath, ...],
    counts: tuple[int, int],
) -> tuple[int, str]:
    if row["status"] == "removed":
        return SENSITIVE_TIER, "file deletion"
    if counts != (row["additions"], row["deletions"]):
        return SENSITIVE_TIER, "incomplete text diff"
    if PurePosixPath(LOCAL_POLICY) in paths:
        return SENSITIVE_TIER, "local policy change"
    if (
        "patch" in row
        and name in PROSE_REPOS
        and all(
            path.suffix.lower() in PROSE_SUFFIXES and not path.is_relative_to("designs")
            for path in paths
        )
    ):
        return 0, "prose-only change"
    return 1, "default review"


def file_reasons(
    name: str, row: ChangedFile, rules: list[Rule], local: list[Rule]
) -> list[tuple[int, str]]:
    path = row["filename"]
    paths = PurePosixPath(path), PurePosixPath(row.get("previous_filename", path))
    changed = PATCH_LINES.findall(row.get("patch", ""))
    lines = [line for _, line in changed]
    added = sum(mark == "+" for mark, _ in changed)
    base = baseline(name, row, paths, (added, len(lines) - added))
    matches = [(rule.tier, rule.reason) for rule in rules if rule.matches(paths, lines)]
    matches.append(base)
    floor = max(tier for tier, _ in matches)
    additions = [
        (rule.tier, rule.reason) for rule in local if rule.matches(paths, lines)
    ]
    if any(tier < floor for tier, _ in additions):
        raise Invalid(LOWER_TIER)
    return matches + additions


def classify(
    name: str, rows: list[ChangedFile], rules: list[Rule], local: list[Rule]
) -> tuple[int, list[str]]:
    sensitive = {rule.selector for rule in rules if rule.tier == SENSITIVE_TIER}
    if any(r.tier == 0 or (r.tier == 1 and r.selector in sensitive) for r in local):
        raise Invalid(LOWER_TIER)
    matches = [
        match for row in rows for match in file_reasons(name.lower(), row, rules, local)
    ]
    tier = max((level for level, _ in matches), default=1)
    reasons = dict.fromkeys(reason for level, reason in matches if level == tier)
    return tier, list(reasons) or ["empty diff"]


def snapshot(client: httpx.Client, pull: str) -> tuple[str, str, int]:
    meta = gh_app.json_object(client.get(pull).raise_for_status())
    head = SHA.fullmatch(review_post.pull_head(meta) or "")
    base = SHA.fullmatch(review_post.pull_head({"head": meta.get("base")}) or "")
    count = meta.get("changed_files")
    if head and base and type(count) is int and 0 <= count <= FILE_LIMIT:
        return head[0], base[0], count
    message = "GitHub returned invalid pull request metadata or too many files"
    raise Invalid(message)


def read_diff(client: httpx.Client, pull: str, count: int) -> list[ChangedFile]:
    rows: list[ChangedFile] = []
    for page, start in enumerate(range(0, count, PAGE_SIZE), 1):
        params = {"per_page": PAGE_SIZE, "page": page}
        response = client.get(f"{pull}/files", params=params)
        batch: object = response.raise_for_status().json()
        if not isinstance(batch, list) or len(batch) != min(PAGE_SIZE, count - start):
            message = "GitHub returned an incomplete file list"
            raise Invalid(message)
        rows.extend(cast("list[ChangedFile]", batch))
    return rows


def post(
    client: httpx.Client, repository: str, number: int, rules: list[Rule]
) -> tuple[int, list[str]]:
    root = f"/repos/{repository}"
    pull = f"{root}/pulls/{number}"
    head, base, count = snapshot(client, pull)
    rows = read_diff(client, pull, count)
    policy = client.get(
        f"{root}/contents/{LOCAL_POLICY}",
        params={"ref": base},
        headers={"Accept": "application/vnd.github.raw+json"},
    )
    text = (
        ""
        if policy.status_code == httpx.codes.NOT_FOUND
        else policy.raise_for_status().text
    )
    tier, reasons = classify(
        repository.split("/")[1], rows, rules, load_rules(text, "local")
    )
    if snapshot(client, pull) != (head, base, count):
        message = "the pull request changed since its diff was read"
        raise Invalid(message)
    status = {
        "context": "review-tier",
        "state": "success",
        "description": f"tier {tier}: {reasons[0]}",
    }
    client.post(f"{root}/statuses/{head}", json=status).raise_for_status()
    return tier, reasons


def repository_slug() -> str:
    code, url = repo.git("remote", "get-url", "origin")
    value = repo_name(url.strip().replace(":", "/")) or ""
    if (
        code
        or not repo.is_tbhb_dev_url(url)
        or not gh_app.REPO_PATTERN.fullmatch(value)
    ):
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
    tier_parser = parser.add_subparsers(required=True).add_parser("tier")
    tier_parser.add_argument("number", type=int)
    tier_parser.add_argument("--config", default=os.environ.get(POLICY_ENV), type=Path)
    tier_parser.add_argument("--account")
    args = parser.parse_args(argv)
    try:
        if args.config is None or args.number < 1:
            message = f"a positive PR number and --config or {POLICY_ENV} are required"
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
