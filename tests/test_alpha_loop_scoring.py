"""Head-bound synthetic comparison rows and persisted score summaries."""

# cspell:words unscored

from dataclasses import replace

import pytest
from test_alpha_loop_rules import APPROVED, BUDGET, HEAD, MERGED, PASSED, PR, REPO

from tbhbdev.alpha.loop import runner, scoring, store


@pytest.mark.parametrize(
    ("rule", "key", "pr", "expected"),
    [
        ("R0", "sweep:12", PR, store.Result.AGREE),
        ("R1", f"review:{REPO}#7@{HEAD}", PR, store.Result.NO_CHANGE),
        ("R1", f"review:{REPO}#7@{HEAD}", PASSED, store.Result.AGREE),
        ("R1", f"review:{REPO}#7@{HEAD}", APPROVED, store.Result.AGREE),
        ("R2", f"approve:{REPO}#7@{HEAD}", APPROVED, store.Result.AGREE),
        ("R2", f"approve:{REPO}#7@{HEAD}", PR, store.Result.NO_CHANGE),
        ("R4", f"merge:{REPO}#7@{HEAD}", MERGED, store.Result.AGREE),
        ("R4", f"merge:{REPO}#7@{HEAD}", APPROVED, store.Result.NO_CHANGE),
        (
            "R5",
            f"verify:{REPO}#7",
            replace(MERGED, verification_passed=True),
            store.Result.AGREE,
        ),
        ("R5", f"verify:{REPO}#7", MERGED, store.Result.NO_CHANGE),
        (
            "R1",
            f"review:{REPO}#7@{HEAD}",
            replace(PR, head_sha="b" * 40),
            store.Result.NO_CHANGE,
        ),
        (
            "R2",
            f"approve:{REPO}#7@{HEAD}",
            replace(APPROVED, head_sha="b" * 40),
            store.Result.NO_CHANGE,
        ),
        (
            "R4",
            f"merge:{REPO}#7@{HEAD}",
            replace(PR, facts=replace(PR.facts, lifecycle="closed")),
            store.Result.DISAGREE,
        ),
        ("R1", f"review:{REPO}#8@{HEAD}", PR, store.Result.NO_CHANGE),
        ("R9", f"escalate:{REPO}#7@{HEAD}:age", APPROVED, store.Result.NO_CHANGE),
    ],
)
def test_score_rows(
    rule: runner.rules.Rule,
    key: str,
    pr: runner.rules.PullRequest,
    expected: store.Result,
) -> None:
    assert (
        scoring.score(
            store.ShadowIntent(key, REPO, rule), runner.Snapshot(BUDGET, (pr,))
        )
        == expected
    )


def test_explicit_evidence_is_key_scoped() -> None:
    intent = store.ShadowIntent("collect:example-run", REPO, "R7")
    snapshot = runner.Snapshot(
        BUDGET, effects=(runner.Effect("collect:another-run", store.Result.AGREE),)
    )
    assert scoring.score(intent, snapshot) == store.Result.NO_CHANGE
    snapshot = replace(
        snapshot, effects=(runner.Effect(intent.key, store.Result.DISAGREE),)
    )
    assert scoring.score(intent, snapshot) == store.Result.DISAGREE


def test_report_groups_rules_and_repositories_without_counting_no_change_as_final() -> (
    None
):
    db = store.Store(":memory:")
    for number, result in enumerate(
        (store.Result.AGREE, store.Result.DISAGREE, store.Result.NO_CHANGE, None), 1
    ):
        key = f"merge:{REPO}#{number}@{HEAD}"
        assert db.record_shadow(store.ShadowIntent(key, REPO, "R4"), 1)
        if result is not None:
            assert db.score_shadow(key, 2, result) == store.ScoreStatus.SCORED
    other = "example-org/other"
    assert db.record_shadow(
        store.ShadowIntent(f"escalate:{other}#1@{HEAD}:age", other, "R9"), 1
    )
    assert scoring.summarize(db) == scoring.Summary(
        (
            scoring.RuleCounts(REPO, "R4", 4, 1, 1, 1, 1),
            scoring.RuleCounts(other, "R9", 1, 0, 0, 0, 1),
        ),
        1,
    )
    assert scoring.summarize(db).counts[0].unscored == 1
    db.close()


def test_scoring_searches_past_unrelated_objects() -> None:
    intent = store.ShadowIntent(f"merge:{REPO}#7@{HEAD}", REPO, "R4")
    snapshot = runner.Snapshot(BUDGET, (replace(PR, number=8), MERGED))
    assert scoring.score(intent, snapshot) == store.Result.AGREE


@pytest.mark.parametrize(
    ("agree", "disagree", "eligible", "review"),
    [
        (20, 0, True, False),
        (19, 0, False, False),
        (0, 20, True, True),
        (19, 1, True, True),
        (20, 1, True, True),
    ],
)
def test_promotion_requires_separate_disagreement_review(
    agree: int,
    disagree: int,
    eligible: bool,
    review: bool,
) -> None:
    counts = scoring.RuleCounts(REPO, "R2", agree + disagree, agree, disagree, 0, 0)
    assert counts.eligible is eligible
    assert counts.needs_review is review


def test_empty_report() -> None:
    db = store.Store(":memory:")
    assert scoring.summarize(db) == scoring.Summary((), 0)
    db.close()


def test_report_sorts_repository_ties_then_natural_rule_order() -> None:
    db = store.Store(":memory:")
    for repo in ("example-org/z-repo", REPO):
        for rule, key in (
            ("R10", f"notify:{repo}#1@{HEAD}:ci_red"),
            ("R2", f"approve:{repo}#1@{HEAD}"),
        ):
            assert db.record_shadow(store.ShadowIntent(key, repo, rule), 1)
    assert [(row.repository, row.rule) for row in scoring.summarize(db).counts] == [
        (REPO, "R2"),
        (REPO, "R10"),
        ("example-org/z-repo", "R2"),
        ("example-org/z-repo", "R10"),
    ]
    db.close()


def test_summary_reads_one_statement_snapshot() -> None:
    trace: list[str] = []
    db = store.Store(":memory:")
    connection = db._connection  # noqa: SLF001 -- trace SQL at the report boundary
    assert connection is not None
    connection.set_trace_callback(trace.append)
    for number in (1, 2):
        key = f"approve:{REPO}#{number}@{HEAD}"
        assert db.record_shadow(store.ShadowIntent(key, REPO, "R2"), 1)
    trace.clear()
    assert scoring.summarize(db).counts == (
        scoring.RuleCounts(REPO, "R2", 2, 0, 0, 0, 2),
    )
    assert len(trace) == 1
    assert "JOIN actions" in trace[0]
    db.close()
