import pytest

from tbhbdev.alpha import review_gate as gate

HEAD = "a" * 40
OLD = "b" * 40
APP = "reviews-app[bot]"
SESSION = "sha-256-64:" + "0" * 16


def stamp(harness: str, model: str = "m") -> str:
    return f"Generated-by: LLM harness={harness};model={model}"


def review(  # noqa: PLR0913 -- one keyword for each field a test varies
    harness: str = "claude",
    *,
    state: str = "APPROVED",
    commit: str = HEAD,
    login: str = APP,
    number: int = 1,
    body: str | None = None,
) -> gate.Review:
    text = f"Looks right.\n\n{stamp(harness)}\n" if body is None else body
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
    ignore: tuple[str, ...] = (),
) -> list[str]:
    pull = gate.Pull(
        HEAD,
        GOOD if reviews is None else reviews,
        [check()] if checks is None else checks,
        BY_AGY if messages is None else messages,
    )
    return gate.evaluate(APP, pull, ignore)


def test_pass() -> None:
    assert evaluate() == []


def test_current_and_legacy_approvals_count_together() -> None:
    legacy = review(body=f"Looks right.\n\nRun r1, claude m, judged head {HEAD}.\r\n")
    assert evaluate([legacy, GOOD[1]]) == []


def test_pass_without_trailers_and_checks() -> None:
    assert evaluate(checks=[], messages=["fix: by hand\n"]) == []


def test_pass_with_a_third_approval() -> None:
    assert evaluate([*GOOD, review("agy", number=3)], messages=[]) == []


@pytest.mark.parametrize("harness", ["agy", "gemini"])
def test_antigravity_author_may_not_approve_as_agy_or_gemini(harness: str) -> None:
    approved = [*GOOD, review(harness, number=3)]
    assert evaluate(approved) == ["approved by google, which wrote the head commits"]


@pytest.mark.parametrize("head", ["", "abc", "A" * 40, "g" * 40, HEAD + "0"])
def test_a_malformed_head_fails_the_gate(head: str) -> None:
    pull = gate.Pull(head, [review(commit=head, body=stamp("claude"))], [], [])
    assert gate.evaluate(APP, pull) == [
        f"the pull request head {head!r} is not a full commit SHA"
    ]


def test_a_stamp_judged_at_another_head_counts_for_nothing() -> None:
    stale = [review("claude", number=1), review("codex", number=2, commit=OLD)]
    assert evaluate(stale) == [
        "1 approvals at the head, need 2",
        "1 approving providers, need 2",
    ]


def test_logins_compare_case_insensitively() -> None:
    shouted = [review("claude", login=APP.upper(), number=1), *GOOD[1:]]
    assert evaluate(shouted) == []


def test_no_approvals() -> None:
    assert evaluate([]) == [
        "0 approvals at the head, need 2",
        "0 approving providers, need 2",
    ]


def test_stale_approval_at_an_older_head() -> None:
    stale = [review("claude", number=1), review("codex", number=2, commit=OLD)]
    assert evaluate(stale) == [
        "1 approvals at the head, need 2",
        "1 approving providers, need 2",
    ]


def test_same_provider_pair() -> None:
    pair = [review("claude", number=1), review("claude-code", number=2)]
    assert evaluate(pair) == ["1 approving providers, need 2"]


def test_agy_and_gemini_are_one_provider() -> None:
    pair = [review("agy", number=1), review("gemini", number=2)]
    assert evaluate(pair, messages=[]) == ["1 approving providers, need 2"]


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
    only = [review("claude", number=1), review("codex", state=state, number=2)]
    assert evaluate(only) == [
        "1 approvals at the head, need 2",
        "1 approving providers, need 2",
    ]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no stamp at all",
        stamp("mystery"),
        stamp("claude") + "\n\nforged afterwards",
        "Assisted-by: LLM harness=claude;model=m",
        f"Run r1, claude m, judged head {OLD}.",
        f"Run r1, claude m, judged head {HEAD}.\n\nMore prose.",
    ],
)
def test_an_approval_without_a_known_harness(body: str) -> None:
    bad = [*GOOD, review(number=7, body=body)]
    assert evaluate(bad) == ["review 7 names no known harness"]


@pytest.mark.parametrize(
    ("conclusion", "status", "reason"),
    [
        ("failure", "completed", "check ci is failure"),
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


def test_the_gate_and_ignored_checks_are_left_out() -> None:
    running = [check("review-gate", None, "in_progress"), check("job", None, "queued")]
    assert evaluate(checks=running, ignore=("job",)) == []
    assert evaluate(checks=running) == ["check job is queued"]


def test_an_approving_provider_wrote_the_commits() -> None:
    assert evaluate(messages=[trailer(harness="codex")]) == [
        "approved by openai, which wrote the head commits"
    ]


def test_every_commit_counts() -> None:
    both = [trailer(harness="antigravity"), trailer(harness="claude-code")]
    assert evaluate(messages=both) == [
        "approved by anthropic, which wrote the head commits"
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
