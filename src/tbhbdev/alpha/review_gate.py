# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Evaluate reviews-verified for a pull request at its head."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypedDict

from tbhbdev.alpha import identity

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

CONTEXT = "reviews-verified"
APP = "tbhb-dev-agent-reviews[bot]"
TIER_TWO = 2
TIER_TWO_APPROVALS = 2
UNKNOWN_BUILDER_PROVIDERS = 2
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
# The run stamp precedes the trailer on new posts. Older stamps omit the variant.
STAMP = re.compile(
    r"(?:^|\n)Run (\S+), (\S+) \S+, judged head ([0-9a-f]{40})(?:, variant (\S+))?\."
    rf"(?:\n\n{identity.KEY_GEN}: (?P<trailer>LLM [^\n]+))?\Z"
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
    globs = [glob for line in policy.splitlines() if (glob := line.strip())]
    match = any(
        PurePosixPath(path).full_match(glob) for path in paths for glob in globs
    )
    return TIER_TWO if match else 1


def provider_of(harness: str) -> str | None:
    """The provider of a harness name, or None for an unknown one."""
    return PROVIDERS.get(harness.lower().partition(":")[0])


def author_providers(messages: Sequence[str]) -> tuple[set[str], list[str], bool]:
    """Return builder providers, invalid harnesses and whether any are unknown."""
    found: set[str] = set()
    invalid: list[str] = []
    unknown = not messages
    for message in messages:
        known = False
        for key, value in identity.llm_trailers(message):
            if key != identity.KEY_GEN:
                continue
            harness = dict(identity.ATTR_PAIR.findall(value)).get("harness", "")
            provider = provider_of(harness)
            if provider:
                found.add(provider)
                known = True
            else:
                invalid.append(harness or "(none)")
        unknown |= not known
    return found, invalid, unknown


def latest_reviews(
    reviews: Sequence[Review], head: str
) -> dict[str, tuple[str, str, str | None]]:
    """Read the newest state, harness and variant for each stamped run."""
    return {
        stamp[1]: (review["state"], stamp[2], stamp[4])
        for review in sorted(reviews, key=lambda item: item["id"])
        if (stamp := STAMP.search(review["body"].rstrip()))
        and stamp[3] == head
        and (not stamp["trailer"] or identity.check_value(stamp["trailer"]) is None)
    }


def approval_reasons(
    approvals: Sequence[tuple[str | None, str | None]], tier: int, *, unknown: bool
) -> Iterator[str]:
    """Check the approval counts for the tier and builder attribution."""
    providers = len({provider for provider, _ in approvals})
    if unknown and providers < UNKNOWN_BUILDER_PROVIDERS:
        yield (
            "unknown builder needs approvals at the head from "
            f"{UNKNOWN_BUILDER_PROVIDERS} providers (found {providers})"
        )
    if tier == TIER_TWO:
        variants = len({variant for _, variant in approvals})
        if variants < TIER_TWO_APPROVALS:
            yield (
                f"{variants} independent approval variants at the head, "
                f"need {TIER_TWO_APPROVALS}"
            )
    elif not unknown and not approvals:
        yield "no independent approval at the head"


def independent_approvals(
    reviews: Sequence[Review], pull: Pull, authors: set[str]
) -> list[tuple[str | None, str | None]]:
    """Approvals from eligible providers, with variants required in tier 2."""
    eligible = set(PROVIDERS.values()) - authors
    return [
        (provider, variant)
        for state, harness, variant in latest_reviews(reviews, pull.head).values()
        if state == "APPROVED"
        and (provider := provider_of(harness)) in eligible
        and (pull.tier != TIER_TWO or variant is not None)
    ]


def review_reasons(app: str, pull: Pull) -> Iterator[str]:
    """Any blocking review or shortfall in independent approvals."""
    authors, invalid, unknown = author_providers(pull.messages)
    yield from (f"a Generated-by trailer names unknown harness {n}" for n in invalid)
    login = app.casefold()
    reviews = [
        item
        for item in pull.reviews
        if item["login"].casefold() == login and item["commit_id"] == pull.head
    ]
    yield from (
        f"review {item['id']} requests changes at the head"
        for item in reviews
        if item["state"] == "CHANGES_REQUESTED"
    )
    approvals = independent_approvals(reviews, pull, authors)
    yield from approval_reasons(approvals, pull.tier, unknown=unknown)


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
    """The reasons a pull request fails at its head; none means it passes."""
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
