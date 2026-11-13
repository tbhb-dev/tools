"""Compare exemptions against a frozen baseline on a synthetic sentence grid."""

import ast
import hashlib
import re
import subprocess
from pathlib import Path

import pytest
import state_guard_baseline as baseline

from tbhbdev.alpha import state_guard

# Sentences that a future exemption targets. The head still judges them as main does.
CANDIDATES = (
    "If the fix is deployed.",
    "Once the fix is deployed.",
    "I'll tell you once the fix is deployed.",
    "The fix isn't deployed.",
    "The fix never merged.",
    "No build is running.",
    "I applied nothing.",
    "Please get the fix merged.",
    "Can you confirm the fix is merged?",
    "Could you get it merged?",
    "I merged none.",
    "I merged nothing.",
    "It hasn't merged",
    "It never merged.",
    "Never merged.",
    "No fix merged.",
    "Nothing merged.",
    "Tell me when it is merged.",
    "Whether the fix is merged.",
    'The word "merged".',
    'Look at "merged" now.',
)

# Each exemption pull request adds its passing sentences here, with the reason.
INTENDED_PASSES: dict[str, str] = {
    sentence.replace("\\t", "\t"): reason
    for sentence, reason in (
        line.split("\t")
        for line in (Path(__file__).parent / "state_guard_intended_passes.tsv")
        .read_text(encoding="utf-8")
        .splitlines()
    )
}

# Sentences with a claim word inside an "after" or "until" condition.
DEFERRED_SENTENCES = tuple(
    sentence
    for sentence, reason in INTENDED_PASSES.items()
    if reason == "a deferred step, not a report"
)

PROBES = (
    "4 deployed services yesterday.",
    "A deployed worker is running.",
    "All done",
    "All merged code into main.",
    "Are you aware it is deployed?",
    "Both merged changes into main.",
    "Both merged today.",
    "Can you believe the fix merged?",
    "Can you confirm the fix is merged?",
    "Can you see that the fix is merged?",
    "Could you get it merged?",
    "Deployed.",
    "Did the fix merge? It merged.",
    "Did you know the fix is deployed?",
    "Did you see that the fix is merged?",
    "Do you think the fix is deployed?",
    "Done.",
    "Everything is deployed.",
    "Fix merged?",
    "How was the fix merged?",
    "I merged none.",
    "I merged nothing.",
    "I'll get it merged and the PR is merged.",
    "I'll tell you once the fix is deployed and merged.",
    "I'll tell you the fix is merged.",
    "If it merged I'm done.",
    "If the fix is deployed, tell me.",
    "Is it true the fix is merged?",
    "Is the fix merged?",
    "Isn't the fix merged?",
    "It hasn't merged",
    "It is merged, right?",
    "It never merged.",
    "It will be merged and deployed.",
    "It will be merged.",
    "It's done",
    "Merged.",
    "My merged fix is deployed.",
    "Neither service deployed nothing.",
    "Neither service has never deployed.",
    "Neither service is not deployed.",
    "Neither service isn't deployed.",
    "Never merged.",
    "No fix merged.",
    "No one merged it and the fix is merged.",
    "No, the fix merged.",
    "Nobody deployed the fix.",
    "Nobody has never merged.",
    "Not everything is deployed.",
    "Not only is the fix merged.",
    "Nothing is deployed but the fix merged.",
    "Nothing merged.",
    "Once the fix is deployed it is merged.",
    "Once the fix is deployed.",
    "Our merged PRs landed.",
    "Please get the fix merged.",
    "Several applied patches to main.",
    "Several merged branches were deleted.",
    "Surely the fix is merged?",
    "Tell me when it is merged.",
    "The 3 deployed workers are up.",
    "The fix is certainly deployed.",
    "The fix is merged?",
    "The fix is not approved or is deployed.",
    "The fix is not approved or is now deployed.",
    "The fix is not merged.",
    "The fix is not not merged.",
    "The fix isn't deployed.",
    "The fix isn't merged and is deployed.",
    "The fix merged!",
    "The fix never merged.",
    "The fix should already be deployed.",
    "The fix was deployed yesterday.",
    "The fix will be deployed, it merged.",
    "The merged PR is merged.",
    "The merged code is live.",
    "The service Never is deployed.",
    "The service Not is deployed.",
    "The site /never is deployed.",
    "The site /not is live.",
    "The two merged PRs are open.",
    "This merged code deployed.",
    "WILL merged.",
    "Wait until the fix is deployed.",
    "What I changed is deployed?",
    "What if the fix is merged?",
    "Whether the fix is merged.",
    "Who knew the fix was deployed?",
    "Why is the fix deployed?",
    "Why wait when the fix is merged?",
    "block",
    "pass ",
    "will merged.",
)

BASE_SENTENCES = (
    "The fix is deployed.",
    "The change merged.",
    "All changes merged.",
    "The deploy is done.",
    "The worker is running.",
    "The patch shipped.",
    "The fix is verified.",
    "The branch landed.",
    *CANDIDATES,
    *PROBES,
    'The   word   "merged".',
    'The\tword\t"merged".',
    "The report describes the merged PR.",
    "The report on the deployed application needs editing.",
)

SUFFIXES = (
    " — can you check?",
    " \u2013 can you check?",
    " - can you check?",
    ", can you check?",
    "; can you check?",
    " (src/app.py unchanged).",
    ', 2 commands printed "please check later".',
    ', "please check later".',
    "; Source: N/A.",
    "; can I read back the result?",
    " although the build failed.",
    " though the build failed.",
    " while the build failed.",
    " whereas the build failed.",
    " unless the fix deployed.",
    " but the build failed.",
    " however the build failed.",
    " yet the build failed.",
    " except the build failed.",
    " and the build failed.",
    "; at 20 requests per review.",
    "; per N workers.",
    "; build 12345 failed.",
)

MUST_BLOCK = (
    "What I changed is deployed — can you check?",
    "The fix isn't not deployed.",
    "The fix is not currently not deployed.",
    "Pat's merged code into main.",
    "The worker is running at 20 requests per review.",
    "The worker is running at 20 requests per operator.",
    "The deploy is done although build 12345 failed.",
    'The fix merged, 2 commands printed "please check later".',
    "The fix merged (src/app.py unchanged).",
    "The fix merged; Source: N/A.",
    "The fix merged; can I read back the result?",
    "All changes merged/verified.",
    "The change merged-into-main.",
)


def generated_corpus() -> set[str]:
    sentences = set(BASE_SENTENCES) | set(MUST_BLOCK) | set(DEFERRED_SENTENCES)
    sentences.update(
        sentence.rstrip(".?") + suffix
        for sentence in BASE_SENTENCES
        for suffix in SUFFIXES
    )
    sentences.update(
        re.sub(r"\b(?:is deployed|merged)\b", replacement, sentence)
        for sentence in BASE_SENTENCES
        for replacement in (
            "isn't not deployed",
            "is not currently not deployed",
            "isn't never deployed",
            "hasn't not merged",
            "merged/verified",
            "merged-into-main",
            "deployed/live",
        )
    )
    sentences.update(
        f"{subject}'s {status} code into main."
        for subject in ("Pat", "Sam", "Alex")
        for status in ("merged", "applied", "deployed")
    )
    sentences.update(
        sentence.rstrip(".") + suffix
        for sentence in BASE_SENTENCES[:8]
        for suffix in (
            " at 20 requests per review.",
            " per N workers.",
            " build 12345 failed.",
        )
    )
    return sentences


def main_blocks(sentence: str) -> bool:
    """Main's judge, fed the renderings the hooks build (normalization included)."""
    try:
        spaced, joined = state_guard.renderings(sentence)
    except state_guard.UndecidableError:
        return True
    return bool(
        baseline.unsupported_claims(spaced) or baseline.unsupported_claims(joined)
    )


def head_blocks(sentence: str) -> bool:
    return bool(state_guard.judged_claims(sentence))


def test_only_intended_simple_sentences_gain_exemptions() -> None:
    corpus = generated_corpus()
    assert len(corpus) >= 300
    new_passes = {s for s in corpus if main_blocks(s) and not head_blocks(s)}
    assert new_passes == INTENDED_PASSES.keys()


@pytest.mark.parametrize("sentence", MUST_BLOCK)
def test_assertion_probes_still_block(sentence: str) -> None:
    assert main_blocks(sentence)
    assert head_blocks(sentence)


@pytest.mark.parametrize(
    "negator", ["not", "never", "no", "nothing", "none", "isn't", "isn\u2019t"]
)
def test_all_negators_count_even_when_separated(negator: str) -> None:
    assert head_blocks(f"{negator} the fix isn't deployed.")


def test_baseline_truncates_and_preserves_sentence_boundaries() -> None:
    sentence = "The fix merged " + "x" * 200
    assert baseline.unsupported_claims(sentence) == [sentence[:160]]
    assert baseline.unsupported_claims("No claim. The fix merged. Output follows.") == [
        "The fix merged."
    ]


def newly_passing() -> set[str]:
    return {s for s in generated_corpus() if main_blocks(s) and not head_blocks(s)}


def exempt_everything(_chunk: str, _match: re.Match[str]) -> bool:
    return True


def test_one_widening_rule_fails_the_differential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert newly_passing() == INTENDED_PASSES.keys()
    monkeypatch.setattr(state_guard, "EXEMPTIONS", (exempt_everything,))
    assert newly_passing() != INTENDED_PASSES.keys()


def test_head_judges_every_other_grid_sentence_as_main_does() -> None:
    assert all(
        main_blocks(s) == head_blocks(s)
        for s in generated_corpus()
        if s not in INTENDED_PASSES
    )


def test_the_exemption_set_is_exactly_the_two_rules() -> None:
    assert (
        state_guard.awaited,
        state_guard.mentioned,
        state_guard.deferred,
    ) == state_guard.EXEMPTIONS


MAIN_SHA = "d604159f7fb7382426be353a940121051fd080d9"
# Hash of the three judge patterns and their flags on main at MAIN_SHA.
MAIN_PATTERNS_SHA256 = (
    "5bb435c05d11f76ccc858fb3de6693bd7e8ac6a512f0c6b7c50e92ad1810421e"
)
JUDGE_PATTERNS = ("CLAIM", "EVIDENCE", "SENTENCE_END")


def pattern_digest(patterns: dict[str, re.Pattern[str]], max_quoted: int) -> str:
    text = "\n".join(
        f"{name}={patterns[name].pattern!r}|{patterns[name].flags}"
        for name in JUDGE_PATTERNS
    )
    return hashlib.sha256(f"{text}\nMAX_QUOTED={max_quoted}".encode()).hexdigest()


DRIFT_HELP = (
    f"The judge patterns or MAX_QUOTED differ from main at {MAIN_SHA}. "
    "To rebase this change on a newer main, set MAIN_SHA to that commit, run "
    "this test's pattern_digest on main's values and paste it into "
    "MAIN_PATTERNS_SHA256, then rebuild tests/state_guard_baseline.py from "
    "main's file."
)


def main_patterns() -> tuple[dict[str, re.Pattern[str]], int] | None:
    """The judge patterns and MAX_QUOTED from main at MAIN_SHA, or None."""
    path = "src/tbhbdev/alpha/state_guard.py"
    root = Path(__file__).parent.parent
    shown = subprocess.run(  # fixed argv, a pinned sha
        ["git", "-C", str(root), "show", f"{MAIN_SHA}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode:
        return None
    found: dict[str, re.Pattern[str]] = {}
    max_quoted = -1
    for node in ast.parse(shown.stdout).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "MAX_QUOTED":
                max_quoted = ast.literal_eval(node.value)
            if name in JUDGE_PATTERNS:
                code = compile(ast.Expression(node.value), name, "eval")
                found[name] = eval(code, {"re": re})  # noqa: S307  # re.compile call
    return found, max_quoted


def head_patterns() -> dict[str, re.Pattern[str]]:
    return {name: getattr(state_guard, name) for name in JUDGE_PATTERNS}


def test_the_head_judge_patterns_are_main_s_at_the_pinned_sha() -> None:
    digest = pattern_digest(head_patterns(), state_guard.MAX_QUOTED)
    assert digest == MAIN_PATTERNS_SHA256, DRIFT_HELP


def test_the_pinned_digest_is_main_s_when_the_commit_is_present() -> None:
    main = main_patterns()
    if main is not None:
        assert pattern_digest(*main) == MAIN_PATTERNS_SHA256, DRIFT_HELP


def test_the_baseline_patterns_are_the_head_s() -> None:
    for name in JUDGE_PATTERNS:
        assert getattr(baseline, name) == getattr(state_guard, name), DRIFT_HELP
    assert baseline.MAX_QUOTED == state_guard.MAX_QUOTED, DRIFT_HELP


REVIEWED = (Path(__file__).parent / "state_guard_must_block.txt").read_text(
    encoding="utf-8"
)


@pytest.mark.parametrize(
    "sentence",
    [
        line.replace("\\n", "\n").replace("\\u200b", "\u200b")
        for line in REVIEWED.splitlines()
    ],
)
def test_reviewed_bypass_sentences_block_on_main_and_head(sentence: str) -> None:
    assert main_blocks(sentence)
    assert head_blocks(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Wait until the fix is deployed.",
        "Wait until a b c is deployed.",
        "  Wait until the fix is deployed.",
        "WAIT UNTIL THE FIX IS DEPLOYED.",
        "Wait until the fix's PR is deployed.",
        "Wait until the fix\u2019s PR is deployed.",
        "Wait until the well-known fix is deployed.",
        'The word "merged".',
        'The term "merged".',
        'The verb "merged".',
        'Look at "is merged" now.',
        "The word \u201cmerged\u201d.",
        'The word "merged\u201d.',
        'The word \u201cmerged".',
        'THE WORD "merged".',
        '  The word "merged".',
    ],
)
def test_each_lead_in_excuses_its_claim_word(sentence: str) -> None:
    assert main_blocks(sentence)
    assert not head_blocks(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Wait until deployed.",
        "Wait until a b c d is deployed.",
        "Wait until, the fix is deployed.",
        "Wait until the fix merged and I deployed it.",
        "Wait until it is merged and then it is deployed.",
        "Wait: the fix is deployed.",
        "I will wait until the fix is deployed.",
        "It will be merged and deployed.",
        "It will be merged, then deployed.",
        "Fix will be merged.",
        "The fix and I will be merged.",
        "The fix will have been merged.",
        "It will not be merged.",
        "The word merged.",
        'The word "merged.',
        'The word "merged .',
        'The word "x" and the fix merged.',
        'The words "merged".',
        'The service called "merged".',
        'Look at the "merged" now.',
        'Look at "x" merged.',
        'The word "merged" and "deployed".',
        "Look at it merged.",
    ],
)
def test_text_beside_a_lead_in_still_blocks(sentence: str) -> None:
    assert main_blocks(sentence)
    assert head_blocks(sentence)


def test_one_excused_claim_does_not_excuse_a_second() -> None:
    sentence = 'The word "merged". The fix is deployed.'
    assert state_guard.unsupported_claims(sentence) == [
        'The word "merged".',
        "The fix is deployed.",
    ]
    both = 'The word "merged" and the fix is deployed.'
    assert state_guard.unsupported_claims(both) == [both]


@pytest.mark.parametrize(
    "sentence",
    [
        "Wait until the fix is deployed.",
        "Wait until the fix is deployed!",
        "Wait until the fix is deployed)",
        'The word "merged"',
        'The word "merged"!',
        'The word "merged" now',
        'The word "merged" NOW.',
        'Look at "merged"   now  .',
    ],
)
def test_each_tail_a_rule_allows_still_passes(sentence: str) -> None:
    assert main_blocks(sentence)
    assert not head_blocks(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Wait until the fix is deployed and verified.",
        "Wait until the fix is deployed, please",
        'Wait until the "fix is deployed".',
        "Wait until the now deployed fix responds.",
        "Wait until it is already deployed.",
        "Wait until it is still deployed.",
        "It will be already merged.",
        "It will be merged now.",
        "It will be merged yet.",
        "It will be merged currently.",
        "It will be merged, current state.",
        "It will be merged today.",
        "It will be merged by Friday.",
        "It will be merged soon.",
        "It will be merged after review.",
        "It will be merged at this point.",
        "It will be merged just now.",
        'The word "merged" now applies.',
        'The word "merged" is true.',
        'The word "merged" is current.',
        'The word "merged", now confirmed.',
        'The word "merged"x.',
        'The word "merged"; done.',
    ],
)
def test_a_qualifier_or_a_trailing_assertion_blocks(sentence: str) -> None:
    assert main_blocks(sentence)
    assert head_blocks(sentence)


@pytest.mark.parametrize(
    "word", ["now", "already", "yet", "still", "currently", "just"]
)
@pytest.mark.parametrize("upper", [False, True])
def test_each_present_word_before_the_claim_blocks_a_wait(
    word: str, *, upper: bool
) -> None:
    shown = word.upper() if upper else word
    assert head_blocks(f"Wait until it is {shown} deployed.")


@pytest.mark.parametrize(
    "word", ["now", "already", "yet", "still", "currently", "just"]
)
@pytest.mark.parametrize("upper", [False, True])
def test_each_present_word_before_the_auxiliary_blocks_a_wait(
    word: str, *, upper: bool
) -> None:
    shown = word.upper() if upper else word
    assert head_blocks(f"Wait until it {shown} is deployed.")


@pytest.mark.parametrize("pronoun", ["I", "we", "you", "he", "she", "they", "it", "WE"])
def test_a_bare_past_verb_after_a_pronoun_blocks_a_wait(pronoun: str) -> None:
    sentence = f"Wait until {pronoun} merged."
    assert main_blocks(sentence)
    assert head_blocks(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Wait until it has merged.",
        "Wait until the fix has merged.",
        "Wait until they have merged.",
        "Wait until it has been merged.",
        "Wait until they have been merged.",
        "Wait until she is merged.",
        "Wait until they are deployed.",
    ],
)
def test_an_auxiliary_before_the_claim_word_makes_a_condition(sentence: str) -> None:
    assert main_blocks(sentence)
    assert not head_blocks(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Wait until Bob merged.",
        "Wait until the fix merged.",
        "Wait until I had merged.",
        "Wait until I've merged.",
        "Wait until it was deployed.",
        "Wait until it was merged.",
        "Wait until a b c has merged.",
        "Wait until has merged.",
    ],
)
def test_any_other_form_after_wait_until_blocks(sentence: str) -> None:
    assert main_blocks(sentence)
    assert head_blocks(sentence)


@pytest.mark.parametrize("pause", [". ", "\n", ".\n", "! ", " ... ", "?\n\n"])
@pytest.mark.parametrize(
    "exempt_sentence", ['The word "merged"', "Wait until it is merged"]
)
def test_text_after_an_exempt_sentence_makes_it_a_claim_again(
    exempt_sentence: str, pause: str
) -> None:
    assert head_blocks(f"{exempt_sentence}{pause}Next.")
    assert not head_blocks(f"{exempt_sentence}{pause}")
    assert not head_blocks(f"{exempt_sentence}{pause}  .  \n")


def test_an_exempt_sentence_before_a_clean_sentence_blocks_alone() -> None:
    assert state_guard.unsupported_claims('The word "merged". Fine.') == [
        'The word "merged".'
    ]
    assert state_guard.unsupported_claims('Fine. The word "merged".') == []
