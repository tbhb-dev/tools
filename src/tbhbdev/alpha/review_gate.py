# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Evaluate reviews-verified for a pull request at its head."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypedDict

from tbhbdev.alpha import identity

if TYPE_CHECKING:
    from collections.abc import Sequence

CONTEXT = "reviews-verified"
APP = "tbhb-dev-agent-reviews[bot]"
TIER_TWO = 2
TIER_TWO_APPROVALS = 2
GOOD_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
PROVIDERS = {
    "claude": "anthropic",
    "claude-code": "anthropic",
    "codex": "openai",
    "agy": "google",
    "antigravity": "google",
    "gemini": "google",
}
HEAD_SHA = re.compile(r"[0-9a-f]{40}")
# The host writes this as the last line. Older stamps omit the variant.
STAMP = re.compile(
    r"Run (\S+), (\S+) \S+, judged head ([0-9a-f]{40})(?:, variant (\S+))?\."
)


class Review(TypedDict):
    id: int
    state: str
    commit_id: str
    body: str
    login: str


class Check(TypedDict):
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class Pull:
    """The facts the check needs about a pull request."""

    head: str
    reviews: Sequence[Review]
    checks: Sequence[Check]
    messages: Sequence[str]
    state: str = "open"
    draft: bool = False
    author: str = ""
    tier: int = 1
    jobs: Sequence[tuple[str, str | None]] = ()


def tier_for(paths: Sequence[str], policy: str) -> int:
    """Match whole repository-relative paths against the base commit's globs."""
    globs = list(map(str.strip, policy.splitlines()))
    return (
        TIER_TWO
        if any(
            PurePosixPath(path).full_match(glob)
            for path in paths
            for glob in globs
            if glob
        )
        else 1
    )


def provider_of(harness: str) -> str | None:
    """The provider of a harness name, or None for an unknown one."""
    return PROVIDERS.get(harness.lower().partition(":")[0])


def author_providers(messages: Sequence[str]) -> tuple[set[str], list[str]]:
    """The providers of the Generated-by trailers, and each unknown harness."""
    found: set[str] = set()
    unknown: list[str] = []
    for message in messages:
        for key, value in identity.llm_trailers(message):
            if key != identity.KEY_GEN:
                continue
            harness = dict(identity.ATTR_PAIR.findall(value)).get("harness", "")
            provider = provider_of(harness)
            if provider:
                found.add(provider)
            else:
                unknown.append(harness or "(none)")
    return found, unknown


def latest_reviews(
    reviews: Sequence[Review], head: str
) -> dict[str, tuple[str, str, str | None]]:
    """Read the newest state, harness and variant for each stamped run."""
    return {
        stamp[1]: (review["state"], stamp[2], stamp[4])
        for review in sorted(reviews, key=lambda item: item["id"])
        if (stamp := STAMP.fullmatch(review["body"].rstrip().rpartition("\n")[2]))
        and stamp[3] == head
    }


def review_reasons(app: str, pull: Pull) -> list[str]:
    """Any blocking review or shortfall in independent approvals."""
    authors, unknown = author_providers(pull.messages)
    reasons = [
        f"a Generated-by trailer names unknown harness {name}" for name in unknown
    ]
    login = app.casefold()
    reviews = [
        item
        for item in pull.reviews
        if item["login"].casefold() == login and item["commit_id"] == pull.head
    ]
    reasons.extend(
        f"review {review['id']} requests changes at the head"
        for review in reviews
        if review["state"] == "CHANGES_REQUESTED"
    )
    eligible = set(PROVIDERS.values()) - authors
    variants = [
        variant
        for state, harness, variant in latest_reviews(reviews, pull.head).values()
        if state == "APPROVED" and provider_of(harness) in eligible
    ]
    if pull.tier != TIER_TWO:
        return (
            reasons if variants else [*reasons, "no independent approval at the head"]
        )
    count = len(set(variants) - {None})
    if count < TIER_TWO_APPROVALS:
        reasons.append(
            f"{count} independent approval variants at the head, "
            f"need {TIER_TWO_APPROVALS}"
        )
    return reasons


def _outcome(check: Check) -> str:
    """The conclusion of a completed check, else its status."""
    if check["status"] == "completed":
        return check["conclusion"] or check["status"]
    return check["status"]


def check_reasons(pull: Pull) -> list[str]:
    """Failures and unfinished check runs other than this check."""
    checks = [check for check in pull.checks if check["name"] != CONTEXT]
    if not checks:
        return ["no check runs at the head"]
    return [
        f"check {check['name']} is {_outcome(check)}"
        for check in checks
        if not (
            check["status"] == "completed" and check["conclusion"] in GOOD_CONCLUSIONS
        )
    ]


def evaluate(app: str, pull: Pull) -> list[str]:
    """The reasons a pull request fails at its head. None means it passes."""
    if not HEAD_SHA.fullmatch(pull.head):
        return [f"the pull request head {pull.head!r} is not a full commit SHA"]
    reasons: list[str] = []
    if pull.state != "open":
        reasons.append("the pull request is not open")
    if pull.draft:
        reasons.append("the pull request is a draft")
    if pull.author.casefold() in {"tbhb", app.casefold()}:
        reasons.append(f"the pull request author is {pull.author}")
    reasons.extend(check_reasons(pull))
    reasons.extend(review_reasons(app, pull))
    reasons.extend(
        f"job {name} ran on runner group {group or '(none)'}"
        for name, group in pull.jobs
        if not (group or "").startswith("tbhb-dev-")
    )
    return reasons
