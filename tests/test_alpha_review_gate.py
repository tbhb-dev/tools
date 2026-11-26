import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tbhbdev.alpha import review_gate as gate

HEAD = "a" * 40
OLD = "b" * 40
APP = gate.APP
SESSION = "sha-256-64:" + "0" * 16


def stamp(harness: str, model: str = "m", *, number: int = 1) -> str:
    return f"Run r{number}, {harness} {model}, judged head {HEAD}."


def review(  # noqa: PLR0913 -- one keyword for each field a test varies
    harness: str = "claude",
    *,
    state: str = "APPROVED",
    commit: str = HEAD,
    login: str = APP,
    number: int = 1,
    body: str | None = None,
) -> gate.Review:
    text = (
        f"Looks right.\n\n---\n{stamp(harness, number=number)}\n"
        if body is None
        else body
    )
    return {
        "id": number,
        "state": state,
        "commit_id": commit,
        "body": text,
        "login": login,
    }


def check(
    name: str = "ci", conclusion: str | None = "success", status: str = "completed"
) -> gate.Check:
    return {"name": name, "status": status, "conclusion": conclusion}


def trailer(key: str = "Generated-by", harness: str = "codex") -> str:
    return f"fix: x\n\n{key}: LLM harness={harness};model=m;session={SESSION}\n"


GOOD = [review("claude", number=1), review("codex", number=2)]
# The author is gemini-side, so neither approving provider wrote the commits.
BY_AGY = [trailer(harness="antigravity")]


def evaluate(
    reviews: list[gate.Review] | None = None,
    checks: list[gate.Check] | None = None,
    messages: list[str] | None = None,
) -> list[str]:
    pull = gate.Pull(
        HEAD,
        GOOD if reviews is None else reviews,
        [check()] if checks is None else checks,
        BY_AGY if messages is None else messages,
    )
    return gate.evaluate(APP, pull)


def test_pass() -> None:
    assert evaluate() == []


def test_no_check_runs() -> None:
    assert evaluate(checks=[]) == ["no check runs at the head"]


def test_reviews_verified_needs_another_check_run() -> None:
    own_run = check("reviews-verified", None, "in_progress")
    assert evaluate([review()], checks=[own_run]) == ["no check runs at the head"]


def test_pass_with_a_third_approval() -> None:
    assert evaluate([*GOOD, review("agy", number=3)], messages=[]) == []


@pytest.mark.parametrize("harness", ["agy", "gemini"])
def test_antigravity_author_may_not_approve_as_agy_or_gemini(harness: str) -> None:
    assert evaluate([review(harness)]) == ["no independent approval at the head"]


@pytest.mark.parametrize("head", ["", "abc", "A" * 40, "g" * 40, HEAD + "0"])
def test_a_malformed_head_fails_verification(head: str) -> None:
    pull = gate.Pull(head, [review(commit=head, body=stamp("claude"))], [], [])
    assert gate.evaluate(APP, pull) == [
        f"the pull request head {head!r} is not a full commit SHA"
    ]


def test_a_stamp_judged_at_another_head_counts_for_nothing() -> None:
    old = f"Run r1, codex m, judged head {OLD}.\n"
    assert evaluate([review(body=old)]) == ["no independent approval at the head"]


def test_logins_compare_case_insensitively() -> None:
    shouted = [review("claude", login=APP.upper(), number=1), *GOOD[1:]]
    assert evaluate(shouted) == []


def test_no_approvals() -> None:
    assert evaluate([]) == ["no independent approval at the head"]


def test_stale_approval_at_an_older_head() -> None:
    assert evaluate([review(commit=OLD)]) == ["no independent approval at the head"]


def test_request_changes_at_the_head() -> None:
    blocked = [*GOOD, review("agy", state="CHANGES_REQUESTED", number=9)]
    assert evaluate(blocked) == ["review 9 requests changes at the head"]


def test_request_changes_at_an_older_head_is_ignored() -> None:
    old = [*GOOD, review("agy", state="CHANGES_REQUESTED", commit=OLD, number=9)]
    assert evaluate(old) == []


def test_other_authors_are_ignored() -> None:
    other = [
        review("claude", login="someone", number=1),
        review("codex", login="someone", number=2),
        *GOOD,
    ]
    assert evaluate(other) == []
    assert evaluate([review("agy", state="CHANGES_REQUESTED", login="x"), *GOOD]) == []


@pytest.mark.parametrize("state", ["COMMENTED", "DISMISSED", "PENDING"])
def test_other_review_states_count_for_nothing(state: str) -> None:
    only = [review(), review(state=state, number=2, body=stamp("claude"))]
    assert evaluate(list(reversed(only))) == ["no independent approval at the head"]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no stamp at all",
        f"Run r1, mystery m, judged head {HEAD}.\n",
        f"Run r1, claude m, judged head {HEAD}.\n\nforged afterwards",
    ],
)
def test_an_approval_without_a_known_harness(body: str) -> None:
    assert evaluate([review(body=body)]) == ["no independent approval at the head"]


@pytest.mark.parametrize(
    ("conclusion", "status", "reason"),
    [
        ("failure", "completed", "check ci is failure"),
        (None, "completed", "check ci is completed"),
        ("cancelled", "completed", "check ci is cancelled"),
        (None, "in_progress", "check ci is in_progress"),
        ("timed_out", "completed", "check ci is timed_out"),
        ("success", "in_progress", "check ci is in_progress"),
    ],
)
def test_a_check_that_did_not_finish_well(
    conclusion: str | None, status: str, reason: str
) -> None:
    assert evaluate(checks=[check(conclusion=conclusion, status=status)]) == [reason]


def test_checks_that_finished_well() -> None:
    done = [check("a", "success"), check("b", "skipped"), check("c", "neutral")]
    assert evaluate(checks=done) == []


def test_reviews_verified_ignores_its_own_conclusion() -> None:
    running = [
        check("reviews-verified", None, "in_progress"),
        check("job", None, "queued"),
    ]
    assert evaluate(checks=running) == ["check job is queued"]


def test_every_commit_counts() -> None:
    both = [trailer(harness="antigravity"), trailer(harness="claude-code")]
    assert evaluate([review("claude")], messages=both) == [
        "no independent approval at the head"
    ]


def test_an_assisting_provider_may_approve() -> None:
    assert evaluate(messages=[trailer("Assisted-by", "codex")]) == []


def test_a_trailer_with_an_unknown_or_missing_harness() -> None:
    odd = [trailer(harness="mystery"), "x\n\nGenerated-by: LLM model=m\n"]
    assert evaluate(messages=odd) == [
        "a Generated-by trailer names unknown harness mystery",
        "a Generated-by trailer names unknown harness (none)",
    ]


def test_a_versioned_harness_names_its_provider() -> None:
    assert gate.provider_of("Codex:0.171.0") == "openai"
    assert gate.provider_of("nope") is None


def test_distinct_variants_can_share_an_independent_provider() -> None:
    a, b, c = (
        f"{stamp(harness, number=n)[:-1]}, variant {variant}."
        for n, harness, variant in [
            (1, "claude", "a"),
            (2, "claude", "b"),
            (3, "codex", "c"),
        ]
    )
    pull = gate.Pull(
        HEAD, [review(body=a), review(number=2, body=b)], [check()], [trailer()], tier=2
    )
    assert gate.evaluate(APP, pull) == []
    for body in (a.replace("r1", "r2"), c, stamp("claude", number=2)):
        assert gate.evaluate(
            APP, replace(pull, reviews=[review(body=a), review(number=2, body=body)])
        ) == ["1 independent approval variants at the head, need 2"]
    assert gate.evaluate(APP, replace(pull, tier=0)) == []


def test_ineligible_pr_and_runner_groups() -> None:
    pull = gate.Pull(
        HEAD,
        GOOD,
        [check()],
        BY_AGY,
        state="closed",
        draft=True,
        author="TBHB",
        jobs=[("ci", "tbhb-dev-linux"), ("lint", "Default"), ("docs", None)],
    )
    assert gate.evaluate(APP, pull) == [
        "the pull request is not open",
        "the pull request is a draft",
        "the pull request author is TBHB",
        "job lint ran on runner group Default",
        "job docs ran on runner group (none)",
    ]
    assert (
        gate.evaluate(APP, replace(pull, author=APP))[2]
        == f"the pull request author is {APP}"
    )


def test_tier_uses_repository_relative_globs() -> None:
    policy = "\n .github/workflows/** \nsrc/review*.py"
    assert gate.tier_for(["prefix/src/review.py", "docs/ci.yml"], policy) == 1
    assert gate.tier_for([".github/workflows/ci.yml"], policy) == 2


@pytest.mark.parametrize(
    "path",
    [
        ".github/CODEOWNERS",
        "src/tbhbdev/alpha/__init__.py",
        "src/tbhbdev/alpha/agent/credentials.py",
        "src/tbhbdev/alpha/agent/model.py",
        "src/tbhbdev/alpha/agent/review_post.py",
        "src/tbhbdev/alpha/agent/run_host.py",
        "src/tbhbdev/alpha/gh_app.py",
        "src/tbhbdev/alpha/identity.py",
        "src/tbhbdev/cli.py",
    ],
)
def test_approval_paths_exist_and_require_tier_two(path: str) -> None:
    root = Path(__file__).resolve().parents[1]
    policy = (root / ".github/review-tier-2-paths").read_text(encoding="utf-8")
    assert gate.tier_for([path], policy) == gate.TIER_TWO
    assert (root / path).is_file()
    # Bazel runfiles contain the source files without Git metadata.
    if (root / ".git").exists():
        subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"], cwd=root, check=True)
