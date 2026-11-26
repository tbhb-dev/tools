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

# The one table from a harness name, as a review body or a trailer spells it,
# to the provider that runs it.
PROVIDERS = {
    "claude": "anthropic",
    "claude-code": "anthropic",
    "codex": "openai",
    "agy": "google",
    "antigravity": "google",
    "gemini": "google",
}
# The review body's last line, which the run host writes: harness, then model.
STAMP = re.compile(r"Run \S+, (\S+) \S+, judged head [0-9a-f]{40}\.")


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


def review_provider(body: str) -> str | None:
    """The provider the review body's last line names, or None."""
    stamp = STAMP.fullmatch(body.rstrip().rpartition("\n")[2])
    return provider_of(stamp[1]) if stamp else None


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


def evaluate(app: str, pull: Pull, ignore: Sequence[str] = ()) -> list[str]:
    """The reasons a pull request fails at its head; none means it passes."""
    reasons: list[str] = []
    ours = [
        r for r in pull.reviews if r["login"] == app and r["commit_id"] == pull.head
    ]
    providers: set[str] = set()
    approvals = 0
    for review in ours:
        if review["state"] == "CHANGES_REQUESTED":
            reasons.append(f"review {review['id']} requests changes at the head")
        elif review["state"] == "APPROVED":
            provider = review_provider(review["body"])
            if provider:
                approvals += 1
                providers.add(provider)
            else:
                reasons.append(f"review {review['id']} names no known harness")
    if approvals < MIN_APPROVALS:
        reasons.append(f"{approvals} approvals at the head, need {MIN_APPROVALS}")
    if len(providers) < MIN_PROVIDERS:
        reasons.append(f"{len(providers)} approving providers, need {MIN_PROVIDERS}")
    skipped = {CONTEXT, *ignore}
    reasons.extend(
        f"check {check['name']} is {check['conclusion'] or check['status']}"
        for check in pull.checks
        if check["name"] not in skipped and check["conclusion"] not in GOOD_CONCLUSIONS
    )
    authors, unknown = author_providers(pull.messages)
    reasons.extend(
        f"approved by {name}, which wrote the head commits"
        for name in sorted(authors & providers)
    )
    reasons.extend(f"a Generated-by trailer names unknown harness {n}" for n in unknown)
    return reasons
