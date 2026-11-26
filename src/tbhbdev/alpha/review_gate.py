# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""The review-gate check of a pull request, as a pure function.

A pull request passes at its current head when the reviews App approved it
twice from two providers, never requested changes, every other check run
finished well, and no approving provider wrote the head commits.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from tbhbdev.alpha import identity

if TYPE_CHECKING:
    from collections.abc import Sequence

CONTEXT = "review-gate"
MIN_APPROVALS = 2
MIN_PROVIDERS = 2
GOOD_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

# Map a trailer harness to its provider.
PROVIDERS = {
    "claude": "anthropic",
    "claude-code": "anthropic",
    "codex": "openai",
    "agy": "google",
    "antigravity": "google",
    "gemini": "google",
}
HEAD_SHA = re.compile(r"[0-9a-f]{40}")
LEGACY_FOOTER = re.compile(r"Run \S+, (\S+) \S+, judged head ([0-9a-f]{40})\.")


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
    """What the check reads of a pull request, at its head."""

    head: str
    reviews: Sequence[Review]
    checks: Sequence[Check]
    messages: Sequence[str]


def provider_of(harness: str) -> str | None:
    """The provider of a harness name, or None for an unknown one."""
    return PROVIDERS.get(harness.lower().partition(":")[0])


def review_provider(body: str, head: str) -> str | None:
    """Read the approving provider from either host-written footer."""
    footer = body.rstrip().rpartition("\n")[2]
    trailers = identity.llm_trailers(footer)
    if trailers and trailers[0][0] == identity.KEY_GEN:
        return provider_of(
            dict(identity.ATTR_PAIR.findall(trailers[0][1])).get("harness", "")
        )
    legacy = LEGACY_FOOTER.fullmatch(footer)
    if legacy and legacy[2] == head:
        return provider_of(legacy[1])
    return None


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


def approvals_at_head(app: str, pull: Pull) -> tuple[int, set[str], list[str]]:
    """The approval count, approving providers and reasons of the App's reviews."""
    reasons: list[str] = []
    providers: set[str] = set()
    approvals = 0
    login = app.casefold()
    for review in pull.reviews:
        if review["login"].casefold() != login or review["commit_id"] != pull.head:
            continue
        if review["state"] == "CHANGES_REQUESTED":
            reasons.append(f"review {review['id']} requests changes at the head")
        elif review["state"] == "APPROVED":
            provider = review_provider(review["body"], pull.head)
            if provider:
                approvals += 1
                providers.add(provider)
            else:
                reasons.append(f"review {review['id']} names no known harness")
    return approvals, providers, reasons


def _outcome(check: Check) -> str:
    """What a check run is: its conclusion once completed, else its status."""
    if check["status"] == "completed":
        return check["conclusion"] or check["status"]
    return check["status"]


def check_reasons(pull: Pull, ignore: Sequence[str]) -> list[str]:
    """The reasons for each check run, bar the gate and ignored ones, not done well."""
    skipped = {CONTEXT, *ignore}
    return [
        f"check {check['name']} is {_outcome(check)}"
        for check in pull.checks
        if check["name"] not in skipped
        and not (
            check["status"] == "completed" and check["conclusion"] in GOOD_CONCLUSIONS
        )
    ]


def author_reasons(pull: Pull, providers: set[str]) -> list[str]:
    """The reasons the head commits' authors disqualify the approvals."""
    authors, unknown = author_providers(pull.messages)
    reasons = [
        f"approved by {name}, which wrote the head commits"
        for name in sorted(authors & providers)
    ]
    reasons.extend(f"a Generated-by trailer names unknown harness {n}" for n in unknown)
    return reasons


def evaluate(app: str, pull: Pull, ignore: Sequence[str] = ()) -> list[str]:
    """The reasons a pull request fails at its head; none means it passes."""
    if not HEAD_SHA.fullmatch(pull.head):
        return [f"the pull request head {pull.head!r} is not a full commit SHA"]
    approvals, providers, reasons = approvals_at_head(app, pull)
    if approvals < MIN_APPROVALS:
        reasons.append(f"{approvals} approvals at the head, need {MIN_APPROVALS}")
    if len(providers) < MIN_PROVIDERS:
        reasons.append(f"{len(providers)} approving providers, need {MIN_PROVIDERS}")
    reasons.extend(check_reasons(pull, ignore))
    reasons.extend(author_reasons(pull, providers))
    return reasons
