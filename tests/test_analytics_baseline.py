"""The nine preceding-release cases and failed history inspections."""

import pytest

from tbhbdev.analytics.decisions.baseline import Facts, choose
from tbhbdev.analytics.model import Verdict


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        pytest.param(
            Facts(staged=True, head_tag="v1.0.0"),
            Verdict(value="v1.0.0", message="baseline: v1.0.0"),
            id="staged_candidate_uses_release_at_head",
        ),
        pytest.param(
            Facts(unstaged=True, head_tag="v1.0.0"),
            Verdict(value="v1.0.0", message="baseline: v1.0.0"),
            id="unstaged_candidate_uses_release_at_head",
        ),
        pytest.param(
            Facts(head_tag="v2.0.0", parent_tag="v1.0.0"),
            Verdict(value="v1.0.0", message="baseline: v1.0.0"),
            id="committed_candidate_excludes_own_release_tag",
        ),
        pytest.param(
            Facts(),
            Verdict(message="first release: no earlier analytics tree"),
            id="first_release_requires_fetched_main_without_analytics",
        ),
        pytest.param(
            Facts(main_has_contract=True),
            Verdict(
                violations=(
                    (
                        "analytics baseline: analytics already shipped "
                        "but no release tag is reachable"
                    ),
                )
            ),
            id="missing_release_tags_after_analytics_shipped_fails_closed",
        ),
        pytest.param(
            Facts(
                main_has_contract=True,
                staged=True,
                head_tag="v1.0.0",
                baseline_has_contract=True,
            ),
            Verdict(value="v1.0.0", message="baseline: v1.0.0"),
            id="populated_baseline_uses_fetched_tags",
        ),
        pytest.param(
            Facts(shallow=True),
            Verdict(violations=("analytics baseline: complete history is required",)),
            id="shallow_checkout_fails_closed",
        ),
        pytest.param(
            Facts(fetched=False),
            Verdict(
                violations=(
                    "analytics baseline: could not fetch release tags and main history",
                )
            ),
            id="unavailable_origin_does_not_print_path",
        ),
        pytest.param(
            Facts(main_has_contract=True, unstaged=True, head_tag="v1.0.0"),
            Verdict(
                violations=(
                    (
                        "analytics baseline: analytics already shipped "
                        "but the baseline has no analytics tree"
                    ),
                )
            ),
            id="older_tag_cannot_replace_shipped_baseline",
        ),
    ],
)
def test_preceding_release_cases(facts: Facts, expected: Verdict) -> None:
    assert choose(facts) == expected


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        (Facts(shallow=None), "could not inspect history"),
        (Facts(main_has_contract=None), "could not inspect main history"),
    ],
)
def test_failed_inspections(facts: Facts, message: str) -> None:
    assert choose(facts) == Verdict(violations=(f"analytics baseline: {message}",))


@pytest.mark.parametrize(
    "facts",
    [Facts(staged=True), Facts(unstaged=True), Facts(staged=True, unstaged=True)],
)
def test_first_release_ignores_candidate_contract(facts: Facts) -> None:
    assert choose(facts) == Verdict(message="first release: no earlier analytics tree")


def test_unchanged_candidate_without_parent_cannot_use_own_tag() -> None:
    assert choose(Facts(head_tag="v1")) == Verdict(
        message="first release: no earlier analytics tree"
    )


def test_parent_release_with_contract() -> None:
    assert choose(
        Facts(main_has_contract=True, parent_tag="v3", baseline_has_contract=True)
    ) == Verdict(value="v3", message="baseline: v3")
