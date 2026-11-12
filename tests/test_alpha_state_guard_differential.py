"""Compare exemptions against a frozen baseline on a synthetic sentence grid."""

import re

import pytest
import state_guard_baseline as baseline

from tbhbdev.alpha import state_guard

INTENDED_PASSES = {
    "If the fix is deployed.": "A conditional status clause.",
    "Once the fix is deployed.": "A future status clause.",
    "I'll tell you once the fix is deployed.": "A future notification condition.",
    "The fix isn't deployed.": "A status negated once.",
    "The fix never merged.": "A status negated by never.",
    "No build is running.": "A status with a negative subject.",
    "I applied nothing.": "A status verb with a negated object.",
    "Please get the fix merged.": "A request to merge.",
    "Can you confirm the fix is merged?": "A request to confirm a status.",
    "Could you get it merged?": "A request to merge.",
    "I merged none.": "A status verb with a negated object.",
    "I merged nothing.": "A status verb with a negated object.",
    "It hasn't merged": "A status negated once.",
    "It never merged.": "A status negated by never.",
    "Never merged.": "A status negated by never.",
    "No fix merged.": "A status with a negative subject.",
    "Nothing merged.": "A status with a negative subject.",
    "Tell me when it is merged.": "A future notification condition.",
    "Whether the fix is merged.": "A conditional status clause.",
}

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
    "Never merged.",
    "No fix merged.",
    "No one merged it and the fix is merged.",
    "No, the fix merged.",
    "Nobody deployed the fix.",
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
    "The fix is merged?",
    "The fix is not merged.",
    "The fix is not not merged.",
    "The fix isn't merged and is deployed.",
    "The fix merged!",
    "The fix should already be deployed.",
    "The fix was deployed yesterday.",
    "The fix will be deployed, it merged.",
    "The merged PR is merged.",
    "The merged code is live.",
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
    *INTENDED_PASSES,
    *PROBES,
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
        ("If the fix is deployed.", "If the fix is deployed, check again."),
        ("The fix isn't deployed.", "The fix isn't not deployed."),
        ("I applied nothing.", "I applied nothing; the fix deployed."),
        ("The fix is no longer deployed.", "The fix is no longer not deployed."),
        ("None were deployed.", "None were deployed although it failed."),
        ("Please get the fix merged.", "Please get the fix merged before lunch."),
    ],
)
def test_each_exemption_has_a_blocking_near_miss(passing: str, blocking: str) -> None:
    assert not head_blocks(passing)
    assert head_blocks(blocking)


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


@pytest.mark.parametrize(
    ("name", "wider"),
    [
        ("CLAIM_QUALIFIER", re.compile("")),
        ("NEGATED_OBJECT", re.compile(r"^\s*(?:nothing|none|today)\b[\s.!?]*$")),
        ("STATUS_END", re.compile(r".*")),
    ],
)
def test_widening_one_pattern_fails_the_differential(
    monkeypatch: pytest.MonkeyPatch, name: str, wider: re.Pattern[str]
) -> None:
    assert newly_passing() == INTENDED_PASSES.keys()
    monkeypatch.setattr(state_guard, name, wider)
    assert newly_passing() != INTENDED_PASSES.keys()
