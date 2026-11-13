"""Compare exemptions against a frozen baseline on a synthetic sentence grid."""

import re
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
INTENDED_PASSES: dict[str, str] = dict(
    line.split("\t")
    for line in (Path(__file__).parent / "state_guard_intended_passes.tsv")
    .read_text(encoding="utf-8")
    .splitlines()
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
    sentences = set(BASE_SENTENCES) | set(MUST_BLOCK)
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


def test_the_exemption_set_is_exactly_the_three_rules() -> None:
    assert (
        state_guard.awaited,
        state_guard.forecast,
        state_guard.mentioned,
    ) == state_guard.EXEMPTIONS


def test_the_baseline_is_main_s_judge() -> None:
    for name in ("CLAIM", "EVIDENCE", "SENTENCE_END"):
        assert getattr(baseline, name) == getattr(state_guard, name)
    assert baseline.MAX_QUOTED == state_guard.MAX_QUOTED


REVIEWED = (Path(__file__).parent / "state_guard_must_block.txt").read_text(
    encoding="utf-8"
)


@pytest.mark.parametrize("sentence", REVIEWED.splitlines())
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
        "It will be merged.",
        "This will be merged.",
        "That will be merged.",
        "The fix will be merged.",
        "  it will be merged.",
        "IT WILL BE MERGED.",
        "The follow-up will be merged.",
        "The fix\u2019s will be merged.",
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
    sentence = "It will be merged. The fix is deployed."
    assert state_guard.unsupported_claims(sentence) == ["The fix is deployed."]
    both = 'The word "merged" and the fix is deployed.'
    assert state_guard.unsupported_claims(both) == [both]
