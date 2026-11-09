"""Derive pull request state from typed observations, without I/O.

The intake layer verifies the base repository through a scoped or reconcile
read. A fork PR sharing a number with a PR in another repository must never
get a reviewer or a merge from an event alone, so unverified facts take
precedence over every other state, including merged and closed.

Review outcomes are ordered oldest to newest: latest means the last element
of ``review_runs_at_head``. Any live run takes precedence over that outcome.
The intake or read layer supplies head-scoped counts: approvals at the current
head only, excluding dismissed reviews and any reviewer whose latest review
there is CHANGES_REQUESTED. It selects the required CI check at that head by
its configured name and owner login, and computes scope from structured reads.
This module does not select checks or count reviews.

An unknown signature requirement is treated as required. Unknown signature
validity holds the request after confirmed blockers and before CI, unless
signatures are explicitly not required.
"""

# cspell:words unreviewed

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

type Lifecycle = Literal["open", "merged", "closed"]
type CIStatus = Literal["absent", "pending", "success", "failure"]
type ReviewOutcome = Literal["live", "approve", "comment", "cannot_review"]


class State(StrEnum):
    UNVERIFIED = "unverified"
    DRAFT = "draft"
    BLOCKED = "blocked"
    SIGNATURE_PENDING = "signature_pending"
    CI_PENDING = "ci_pending"
    CI_RED = "ci_red"
    GREEN_UNREVIEWED = "green_unreviewed"
    REVIEW_RUNNING = "review_running"
    REVIEW_PASSED = "review_passed"
    REVIEW_DECLINED = "review_declined"
    APPROVED = "approved"
    MERGED = "merged"
    CLOSED = "closed"
    OUT_OF_SCOPE = "out_of_scope"


class BlockedReason(StrEnum):
    UNSIGNED = "unsigned"
    DIRTY = "dirty"
    BEHIND = "behind"


@dataclass(frozen=True)
class CIObservation:
    status: CIStatus


@dataclass(frozen=True)
class PullRequestFacts:
    base_repo_verified: bool
    lifecycle: Lifecycle
    is_draft: bool
    in_scope: bool
    required_approvals: int
    approvals_at_head: int
    signatures_required: bool | None
    head_signature_valid: bool | None
    merge_state: str
    ci: CIObservation
    review_runs_at_head: tuple[ReviewOutcome, ...]

    def __post_init__(self) -> None:
        if self.lifecycle not in {"open", "merged", "closed"}:
            message = "lifecycle must be open, merged or closed"
            raise ValueError(message)
        if self.required_approvals < 0:
            message = "required_approvals must be non-negative"
            raise ValueError(message)
        if self.approvals_at_head < 0:
            message = "approvals_at_head must be non-negative"
            raise ValueError(message)


@dataclass(frozen=True)
class PullRequestState:
    state: State
    blocked_reason: BlockedReason | None = None


def _blocked_reason(facts: PullRequestFacts) -> BlockedReason | None:
    if facts.signatures_required is not False and facts.head_signature_valid is False:
        return BlockedReason.UNSIGNED
    if facts.merge_state.lower() == "dirty":
        return BlockedReason.DIRTY
    if facts.merge_state.lower() == "behind":
        return BlockedReason.BEHIND
    return None


def _green_state(facts: PullRequestFacts) -> State:
    if facts.approvals_at_head >= facts.required_approvals:
        return State.APPROVED
    if "live" in facts.review_runs_at_head:
        return State.REVIEW_RUNNING
    if facts.review_runs_at_head:
        return {
            "approve": State.REVIEW_PASSED,
            "comment": State.REVIEW_DECLINED,
            "cannot_review": State.REVIEW_DECLINED,
        }[facts.review_runs_at_head[-1]]
    return State.GREEN_UNREVIEWED


def _classify(  # noqa: PLR0911 -- one return per precedence row
    facts: PullRequestFacts,
) -> tuple[State, BlockedReason | None]:
    if not facts.base_repo_verified:
        return State.UNVERIFIED, None
    if facts.lifecycle == "merged":
        return State.MERGED, None
    if facts.lifecycle == "closed":
        return State.CLOSED, None
    if not facts.in_scope or facts.required_approvals == 0:
        return State.OUT_OF_SCOPE, None
    if facts.is_draft:
        return State.DRAFT, None
    reason = _blocked_reason(facts)
    if reason is not None:
        return State.BLOCKED, reason
    if facts.signatures_required is not False and facts.head_signature_valid is None:
        return State.SIGNATURE_PENDING, None
    if facts.ci.status in {"absent", "pending"}:
        return State.CI_PENDING, None
    if facts.ci.status == "failure":
        return State.CI_RED, None
    return _green_state(facts), None


def derive(facts: PullRequestFacts) -> PullRequestState:
    """Apply the first matching state row, holding unknown required signatures."""
    state, reason = _classify(facts)
    return PullRequestState(state, reason)
