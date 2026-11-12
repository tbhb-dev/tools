"""Compare exemptions against a frozen baseline on a synthetic sentence grid."""

import re

import pytest
import state_guard_baseline as baseline

from tbhbdev.alpha import state_guard

INTENDED_PASSES = {
    "Is the fix deployed?": "A simple status question.",
    "Has the fix been merged?": "A simple status question with an auxiliary.",
    "The fix will be deployed.": "A future status.",
    "The fix would be deployed.": "A conditional modal status.",
    "The fix should be deployed.": "A prospective modal status.",
    "If the fix is deployed.": "A conditional status clause.",
    "Once the fix is deployed.": "A future status clause.",
    "I'll tell you once the fix is deployed.": "A future notification condition.",
    "The fix isn't deployed.": "A status negated once.",
    "The fix never merged.": "A status negated by never.",
    "No build is running.": "A status with a negative subject.",
    "I applied nothing.": "A status verb with a negated object.",
    "The merged branches need editing.": "A determiner and adjective before branches.",
    "Our deployed application needs editing.": "A status adjective before application.",
}

BASE_SENTENCES = (
    "The fix is deployed.",
    "The change merged.",
    "All changes merged.",
    "The deploy is done.",
    "The worker is running.",
    "The patch shipped.",
    "The fix is verified.",
    "The branch landed.",
    *INTENDED_PASSES,
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
MUST_PASS = (
    "The report describes the merged PR.",
    "The report describes the merged branch.",
    "The report on the deployed application needs editing.",
    "Has the fix been merged?",
    "I'll tell you once the fix is deployed.",
)


def generated_corpus() -> set[str]:
    sentences = set(BASE_SENTENCES) | set(MUST_BLOCK) | set(MUST_PASS)
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
    return bool(baseline.unsupported_claims(sentence))


def head_blocks(sentence: str) -> bool:
    return bool(state_guard.unsupported_claims(sentence))


def test_only_intended_simple_sentences_gain_exemptions() -> None:
    corpus = generated_corpus()
    assert len(corpus) >= 300
    new_passes = {s for s in corpus if main_blocks(s) and not head_blocks(s)}
    assert new_passes == INTENDED_PASSES.keys()


@pytest.mark.parametrize("sentence", MUST_BLOCK)
def test_assertion_probes_still_block(sentence: str) -> None:
    assert main_blocks(sentence)
    assert head_blocks(sentence)


@pytest.mark.parametrize("sentence", MUST_PASS)
def test_simple_false_positive_probes_pass(sentence: str) -> None:
    assert not head_blocks(sentence)


@pytest.mark.parametrize(
    ("passing", "blocking"),
    [
        ("Is the fix deployed?", "Is the fix deployed — can you check?"),
        ("  Is the fix deployed?   ", "Is the fix deployed.   "),
        ("The fix will be deployed.", "The fix will be deployed although it failed."),
        ("If the fix is deployed.", "If the fix is deployed, check again."),
        ("The fix isn't deployed.", "The fix isn't not deployed."),
        ("I applied nothing.", "I applied nothing; the fix deployed."),
        ("The merged branches need editing.", "Pat's merged code into main."),
        ("The fix is no longer deployed.", "The fix is no longer not deployed."),
        ("None were deployed.", "None were deployed although it failed."),
        ("Please get the fix merged.", "Please get the fix merged before lunch."),
        ("The fix should be merged.", "The fix should be merged and shipped."),
    ],
)
def test_each_exemption_has_a_blocking_near_miss(passing: str, blocking: str) -> None:
    assert not head_blocks(passing)
    assert head_blocks(blocking)


@pytest.mark.parametrize("quantity", ["the", "a few", "several", "twenty", "21"])
@pytest.mark.parametrize(
    "noun",
    [
        "branch",
        "PR",
        "change",
        "fix",
        "commit",
        "file",
        "artifact",
        "release",
        "package",
        "service",
        "worker",
        "application",
        "ruleset",
        "patch",
        "branches",
        "PRs",
        "changes",
        "fixes",
        "commits",
        "files",
        "artifacts",
        "releases",
        "packages",
        "services",
        "workers",
        "applications",
        "rulesets",
        "patches",
    ],
)
def test_closed_adjective_nouns_and_determiners(quantity: str, noun: str) -> None:
    assert not head_blocks(f"{quantity} merged {noun} need editing.")
    assert head_blocks(f"Pat's merged {noun} into main.")


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


def test_question_exemption_accepts_surrounding_whitespace() -> None:
    chunk = "  Has the fix been merged?   "
    match = state_guard.CLAIM.search(chunk)
    assert match is not None
    assert state_guard.exempt(chunk, match)
