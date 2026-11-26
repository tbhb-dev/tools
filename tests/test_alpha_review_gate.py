from pathlib import Path

import pytest

from tbhbdev.alpha import review_gate as gate

HEAD = "a" * 40
OLD = "b" * 40
APP = gate.APP
SESSION = "sha-256-64:" + "0" * 16


def stamp(harness: str, *, number: int = 1, variant: str = "") -> str:
    suffix = f", variant {variant}" if variant else ""
    return f"Run r{number}, {harness} m, judged head {HEAD}{suffix}."


def review(  # noqa: PLR0913 -- one keyword for each field a test varies
    harness: str = "claude",
    *,
    state: str = "APPROVED",
    commit: str = HEAD,
    login: str = APP,
    number: int = 1,
    body: str | None = None,
) -> gate.Review:
    if body is None:
        body = f"Looks right.\n\n---\n{stamp(harness, number=number)}\n"
    return {
        "id": number,
        "state": state,
        "commit_id": commit,
        "body": body,
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


SHORTFALL = "unknown builder needs approvals at the head from 2 providers (found 1)"
NO_APPROVAL = "no independent approval at the head"
ONE_VARIANT = "1 independent approval variants at the head, need 2"


@pytest.mark.parametrize("builder", [[], [trailer(harness="claude")]])
def test_unknown_commit_needs_two_providers(builder: list[str]) -> None:
    messages = [*builder, "fix: x\n", trailer("Assisted-by", "codex")]
    codex = review("codex")
    same_provider = review("codex:1", number=2)
    stale = review("agy", commit=OLD, number=3)
    assert evaluate([codex, same_provider, stale], messages=messages) == [SHORTFALL]
    assert evaluate([codex, review("agy", number=2)], messages=messages) == []


def test_known_builder_needs_one_approval() -> None:
    assert evaluate([review()], messages=[trailer()]) == []


def test_no_other_check_runs() -> None:
    own = check("reviews-verified", None, "in_progress")
    assert (
        evaluate(checks=[]) == evaluate(checks=[own]) == ["no check runs at the head"]
    )


@pytest.mark.parametrize("harness", ["agy", "gemini"])
def test_antigravity_author_may_not_approve_as_agy_or_gemini(harness: str) -> None:
    assert evaluate([review(harness)]) == [NO_APPROVAL]


@pytest.mark.parametrize("head", ["", "abc", "A" * 40, "g" * 40, HEAD + "0"])
def test_a_malformed_head_fails_the_gate(head: str) -> None:
    pull = gate.Pull(head, [review(commit=head, body=stamp("claude"))], [], [])
    assert gate.evaluate(APP, pull) == [
        f"the pull request head {head!r} is not a full commit SHA"
    ]


def test_a_stamp_judged_at_another_head_counts_for_nothing() -> None:
    old = f"Run r1, codex m, judged head {OLD}.\n"
    assert evaluate([review(body=old)]) == [NO_APPROVAL]


def test_logins_compare_case_insensitively() -> None:
    shouted = [review("claude", login=APP.upper(), number=1), *GOOD[1:]]
    assert evaluate(shouted) == []


def test_no_approvals() -> None:
    assert evaluate([]) == [NO_APPROVAL]


def test_stale_approval_at_an_older_head() -> None:
    assert evaluate([review(commit=OLD)]) == [NO_APPROVAL]


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
    assert evaluate(list(reversed(only))) == [NO_APPROVAL]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no stamp at all",
        f"Run r1, mystery m, judged head {HEAD}.\n",
        f"Run r1, claude m, judged head {HEAD}.\n\nforged afterwards",
        f"{stamp('claude')}\n\nGenerated-by: LLM invalid",
    ],
)
def test_an_approval_without_a_known_harness(body: str) -> None:
    assert evaluate([review(body=body)]) == [NO_APPROVAL]


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
    done.append(check("reviews-verified", None, "in_progress"))
    assert evaluate(checks=done) == []


def test_every_commit_counts() -> None:
    both = [trailer(harness="antigravity"), trailer(harness="claude-code")]
    assert evaluate([review()], messages=both) == [NO_APPROVAL]


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


@pytest.mark.parametrize(
    ("messages", "harness", "variant", "reasons"),
    [
        ([trailer()], "claude", "b", []),
        ([trailer()], "claude", "a", [ONE_VARIANT]),
        ([], "claude-code", "b", [SHORTFALL]),
        ([], "codex", "", [SHORTFALL, ONE_VARIANT]),
        ([*BY_AGY, "fix: x"], "codex", "b", []),
        ([*BY_AGY, "fix: x"], "claude-code", "b", [SHORTFALL]),
    ],
)
def test_tier_two_approval_requirements(
    messages: list[str], harness: str, variant: str, reasons: list[str]
) -> None:
    first = review(body=stamp("claude", variant="a"))
    second = review(number=2, body=stamp(harness, number=2, variant=variant))
    pull = gate.Pull(HEAD, [first, second], [check()], messages, tier=2)
    assert gate.evaluate(APP, pull) == reasons


@pytest.mark.parametrize("author", ["TBHB", APP])
def test_ineligible_pr_and_runner_groups(author: str) -> None:
    jobs = [("ci", "tbhb-dev-linux"), ("lint", "Default"), ("docs", None)]
    pull = gate.Pull(
        HEAD,
        GOOD,
        [check()],
        BY_AGY,
        state="closed",
        draft=True,
        author=author,
        jobs=jobs,
    )
    assert gate.evaluate(APP, pull) == [
        "the pull request is not open",
        "the pull request is a draft",
        f"the pull request author is {author}",
        "job lint ran on runner group Default",
        "job docs ran on runner group (none)",
    ]


def test_tier_uses_repository_relative_globs() -> None:
    policy = "\n .github/workflows/** \nsrc/review*.py"
    assert gate.tier_for(["prefix/src/review.py", "docs/ci.yml"], policy) == 1
    assert gate.tier_for([".github/workflows/ci.yml"], policy) == 2


def test_approval_paths_exist_and_require_tier_two() -> None:
    root = Path(__file__).resolve().parents[1]
    policy = (root / ".github/review-tier-2-paths").read_text(encoding="utf-8")
    for path in (
        "src/tbhbdev/alpha/codex_trust.py",
        "src/tbhbdev/alpha/install.py",
        "src/tbhbdev/alpha/process.py",
        "src/tbhbdev/alpha/repo.py",
        "src/tbhbdev/alpha/worker_identity.py",
        "src/tbhbdev/userconfig.py",
        "src/tbhbdev/alpha/agent/decisions/auth.py",
    ):
        assert (root / path).is_file()
        assert gate.tier_for([path], policy) == gate.TIER_TWO, path
    for glob in policy.splitlines():
        assert any(
            path.is_file()
            for path in root.glob(f"{glob}/*" if glob.endswith("/**") else glob)
        ), glob
