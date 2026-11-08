"""Synthetic observations exercise the state table and its precedence."""

# cspell:words unreviewed

from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest

from tbhbdev.alpha.loop.state import (
    BlockedReason,
    CIObservation,
    Lifecycle,
    PullRequestFacts,
    PullRequestState,
    State,
    derive,
)

GREEN = PullRequestFacts(
    base_repo_verified=True,
    lifecycle="open",
    is_draft=False,
    in_scope=True,
    required_approvals=1,
    approvals_at_head=0,
    signatures_required=True,
    head_signature_valid=True,
    merge_state="clean",
    ci=CIObservation("success"),
    review_runs_at_head=(),
)


@pytest.mark.parametrize(
    ("facts", "expected", "reason"),
    [
        pytest.param(GREEN, State.GREEN_UNREVIEWED, None, id="green-unreviewed"),
        pytest.param(
            replace(GREEN, base_repo_verified=False),
            State.UNVERIFIED,
            None,
            id="unverified-green",
        ),
        pytest.param(
            replace(GREEN, base_repo_verified=False, approvals_at_head=1),
            State.UNVERIFIED,
            None,
            id="unverified-approved",
        ),
        pytest.param(
            replace(GREEN, base_repo_verified=False, lifecycle="merged"),
            State.UNVERIFIED,
            None,
            id="unverified-merged",
        ),
        pytest.param(
            replace(GREEN, base_repo_verified=False, lifecycle="closed"),
            State.UNVERIFIED,
            None,
            id="unverified-closed",
        ),
        pytest.param(
            replace(GREEN, base_repo_verified=False, is_draft=True),
            State.UNVERIFIED,
            None,
            id="unverified-draft",
        ),
        pytest.param(
            replace(GREEN, base_repo_verified=False, in_scope=False),
            State.UNVERIFIED,
            None,
            id="unverified-out-of-scope",
        ),
        pytest.param(
            replace(GREEN, lifecycle="merged", is_draft=True, in_scope=False),
            State.MERGED,
            None,
            id="merged-before-scope-and-draft",
        ),
        pytest.param(
            replace(GREEN, lifecycle="closed", is_draft=True, in_scope=False),
            State.CLOSED,
            None,
            id="closed-before-scope-and-draft",
        ),
        pytest.param(
            replace(GREEN, in_scope=False, is_draft=True, merge_state="dirty"),
            State.OUT_OF_SCOPE,
            None,
            id="scope-before-draft-and-blocked",
        ),
        pytest.param(
            replace(
                GREEN,
                is_draft=True,
                head_signature_valid=False,
                merge_state="dirty",
                ci=CIObservation("failure", "failure"),
                approvals_at_head=1,
                review_runs_at_head=("live",),
            ),
            State.DRAFT,
            None,
            id="draft-before-all-active-states",
        ),
        pytest.param(
            replace(GREEN, head_signature_valid=False, merge_state="dirty"),
            State.BLOCKED,
            BlockedReason.UNSIGNED,
            id="unsigned-before-dirty",
        ),
        pytest.param(
            replace(GREEN, head_signature_valid=False, merge_state="behind"),
            State.BLOCKED,
            BlockedReason.UNSIGNED,
            id="unsigned-before-behind",
        ),
        pytest.param(
            replace(GREEN, merge_state="dirty"),
            State.BLOCKED,
            BlockedReason.DIRTY,
            id="dirty-before-green",
        ),
        pytest.param(
            replace(GREEN, merge_state="behind"),
            State.BLOCKED,
            BlockedReason.BEHIND,
            id="behind-before-green",
        ),
        pytest.param(
            replace(GREEN, merge_state="dirty", ci=CIObservation("pending")),
            State.BLOCKED,
            BlockedReason.DIRTY,
            id="blocked-before-pending",
        ),
        pytest.param(
            replace(GREEN, merge_state="behind", ci=CIObservation("failure")),
            State.BLOCKED,
            BlockedReason.BEHIND,
            id="blocked-before-red",
        ),
        pytest.param(
            replace(GREEN, merge_state="dirty", approvals_at_head=1),
            State.BLOCKED,
            BlockedReason.DIRTY,
            id="blocked-before-approved",
        ),
        pytest.param(
            replace(GREEN, ci=CIObservation("absent"), approvals_at_head=1),
            State.CI_PENDING,
            None,
            id="absent-before-approved",
        ),
        pytest.param(
            replace(GREEN, ci=CIObservation("pending"), review_runs_at_head=("live",)),
            State.CI_PENDING,
            None,
            id="pending-before-live",
        ),
        pytest.param(
            replace(
                GREEN,
                ci=CIObservation("failure", "cancelled"),
                approvals_at_head=1,
                review_runs_at_head=("approve",),
            ),
            State.CI_RED,
            None,
            id="red-before-approved-and-review",
        ),
        pytest.param(
            replace(GREEN, approvals_at_head=1, review_runs_at_head=("live",)),
            State.APPROVED,
            None,
            id="approvals-before-live",
        ),
        pytest.param(
            replace(GREEN, required_approvals=2, approvals_at_head=1),
            State.GREEN_UNREVIEWED,
            None,
            id="approvals-below-count",
        ),
        pytest.param(
            replace(GREEN, required_approvals=2, approvals_at_head=2),
            State.APPROVED,
            None,
            id="approvals-at-count",
        ),
        pytest.param(
            replace(GREEN, required_approvals=2, approvals_at_head=3),
            State.APPROVED,
            None,
            id="approvals-above-count",
        ),
        pytest.param(
            replace(GREEN, required_approvals=0),
            State.GREEN_UNREVIEWED,
            None,
            id="no-required-approvals",
        ),
        pytest.param(
            replace(GREEN, required_approvals=0, approvals_at_head=1),
            State.GREEN_UNREVIEWED,
            None,
            id="no-requirement-with-approval",
        ),
        pytest.param(
            replace(GREEN, required_approvals=0, review_runs_at_head=("live",)),
            State.REVIEW_RUNNING,
            None,
            id="no-requirement-live",
        ),
        pytest.param(
            replace(GREEN, review_runs_at_head=("live",)),
            State.REVIEW_RUNNING,
            None,
            id="live",
        ),
        pytest.param(
            replace(GREEN, review_runs_at_head=("live", "approve")),
            State.REVIEW_RUNNING,
            None,
            id="earlier-live-before-latest",
        ),
        pytest.param(
            replace(GREEN, review_runs_at_head=("approve", "live")),
            State.REVIEW_RUNNING,
            None,
            id="latest-live",
        ),
        pytest.param(
            replace(GREEN, review_runs_at_head=("approve",)),
            State.REVIEW_PASSED,
            None,
            id="review-passed",
        ),
        pytest.param(
            replace(GREEN, review_runs_at_head=("comment", "approve")),
            State.REVIEW_PASSED,
            None,
            id="latest-approve",
        ),
        pytest.param(
            replace(GREEN, review_runs_at_head=("approve", "comment")),
            State.REVIEW_DECLINED,
            None,
            id="latest-comment",
        ),
        pytest.param(
            replace(GREEN, review_runs_at_head=("approve", "cannot_review")),
            State.REVIEW_DECLINED,
            None,
            id="latest-cannot-review",
        ),
        pytest.param(
            replace(GREEN, merge_state="blocked"),
            State.GREEN_UNREVIEWED,
            None,
            id="approval-or-check-blocked",
        ),
        pytest.param(
            replace(GREEN, merge_state="unknown"),
            State.GREEN_UNREVIEWED,
            None,
            id="unknown-merge-state",
        ),
        pytest.param(
            replace(GREEN, merge_state="other"),
            State.GREEN_UNREVIEWED,
            None,
            id="other-merge-state",
        ),
        pytest.param(
            replace(GREEN, signatures_required=False, head_signature_valid=False),
            State.GREEN_UNREVIEWED,
            None,
            id="signatures-not-required",
        ),
        pytest.param(
            replace(GREEN, signatures_required=None, head_signature_valid=False),
            State.GREEN_UNREVIEWED,
            None,
            id="signature-requirement-unknown",
        ),
    ],
)
def test_state_table(
    facts: PullRequestFacts, expected: State, reason: BlockedReason | None
) -> None:
    assert derive(facts) == PullRequestState(expected, reason, signature_unknown=False)


@pytest.mark.parametrize("signatures_required", [True, False, None])
@pytest.mark.parametrize("validity", [True, False, None])
def test_signature_unknown_is_independent_of_the_signature_requirement(
    signatures_required: bool | None, validity: bool | None
) -> None:
    result = derive(
        replace(
            GREEN,
            signatures_required=signatures_required,
            head_signature_valid=validity,
        )
    )
    assert result.signature_unknown is (validity is None)
    if signatures_required and validity is False:
        assert (result.state, result.blocked_reason) == (
            State.BLOCKED,
            BlockedReason.UNSIGNED,
        )
    else:
        assert (result.state, result.blocked_reason) == (State.GREEN_UNREVIEWED, None)


@pytest.mark.parametrize("lifecycle", ["open", "merged", "closed"])
def test_unknown_signature_survives_unverified_and_terminal_states(
    lifecycle: Lifecycle,
) -> None:
    result = derive(replace(GREEN, lifecycle=lifecycle, head_signature_valid=None))
    assert result.signature_unknown is True
    unverified = derive(
        replace(
            GREEN,
            lifecycle=lifecycle,
            base_repo_verified=False,
            head_signature_valid=None,
        )
    )
    assert unverified == PullRequestState(
        State.UNVERIFIED, None, signature_unknown=True
    )


@pytest.mark.parametrize(
    ("required", "approvals", "message"),
    [
        (-1, 0, "required_approvals must be non-negative"),
        (0, -1, "approvals_at_head must be non-negative"),
    ],
)
def test_negative_counts_are_rejected_at_construction(
    required: int, approvals: int, message: str
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        replace(GREEN, required_approvals=required, approvals_at_head=approvals)


@pytest.mark.parametrize("lifecycle", ["unknown", "", "OPEN"])
def test_unknown_lifecycle_is_rejected_at_construction(lifecycle: str) -> None:
    with pytest.raises(ValueError, match=r"^lifecycle must be open, merged or closed$"):
        replace(GREEN, lifecycle=cast("Lifecycle", lifecycle))


@pytest.mark.parametrize(
    ("observation", "field"),
    [
        (GREEN, "is_draft"),
        (CIObservation("success"), "status"),
        (PullRequestState(State.DRAFT), "state"),
    ],
)
def test_observations_and_results_are_frozen(observation: object, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(observation, field, None)


def test_ci_conclusion_is_retained_without_interpreting_text() -> None:
    observation = CIObservation("failure", "synthetic conclusion")
    assert observation.conclusion == "synthetic conclusion"
    assert derive(replace(GREEN, ci=observation)).state is State.CI_RED
    assert CIObservation("absent").conclusion is None


def test_result_defaults_and_enum_values() -> None:
    assert PullRequestState(State.DRAFT).blocked_reason is None
    assert PullRequestState(State.DRAFT).signature_unknown is False
    assert {member.value for member in State} == {
        "unverified",
        "draft",
        "blocked",
        "ci_pending",
        "ci_red",
        "green_unreviewed",
        "review_running",
        "review_passed",
        "review_declined",
        "approved",
        "merged",
        "closed",
        "out_of_scope",
    }
    assert {member.value for member in BlockedReason} == {"unsigned", "dirty", "behind"}
