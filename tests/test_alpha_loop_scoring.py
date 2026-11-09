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
            store.Result.DISAGREE,
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
            assert db.score_shadow(key, 2, result)
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


def test_missing_action_fails_report_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    db = store.Store(":memory:")
    assert db.record_shadow(store.ShadowIntent(f"merge:{REPO}#7@{HEAD}", REPO, "R4"), 1)

    def missing(_key: str) -> None:
        return None

    monkeypatch.setattr(db, "action", missing)
    with pytest.raises(store.StoreError) as caught:
        scoring.summarize(db)
    assert caught.value.code == store.ErrorCode.CORRUPT
    db.close()


def test_scoring_searches_past_unrelated_objects() -> None:
    intent = store.ShadowIntent(f"merge:{REPO}#7@{HEAD}", REPO, "R4")
    snapshot = runner.Snapshot(BUDGET, (replace(PR, number=8), MERGED))
    assert scoring.score(intent, snapshot) == store.Result.AGREE
