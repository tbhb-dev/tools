"""Compare exemptions against a frozen baseline on a synthetic sentence grid."""

import re

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
    "I applied nothing.",
    "Please get the fix merged.",
    "Can you confirm the fix is merged?",
    "Could you get it merged?",
    "I merged nothing.",
    "It hasn't merged",
    "It never merged.",
    "Never merged.",
    "No fix merged.",
    "Nothing merged.",
    "Tell me when it is merged.",
    "Whether the fix is merged.",
)

# The grid sentences that gain an exemption, each with the rule that exempts it.
# The grid derives variants of each root (suffixes, replaced status words), and a
# variant passes only through its root's rule.
TIMED = "a status word after a time or condition word"
MODAL = "a status word after a modal or 'to be'"
NEGATED = "a status word in a clause with one negation"
RULE_NAMES = {TIMED: "timed", MODAL: "modal", NEGATED: "negated"}
INTENDED_PASSES: dict[str, str] = {
    "I applied nothing.": NEGATED,
    "I merged nothing.": NEGATED,
    "I'll tell you once the fix is deployed and merged.": TIMED,
    "I'll tell you once the fix is deployed.": TIMED,
    "If it merged I'm done.": TIMED,
    "If the fix is deployed, tell me.": TIMED,
    "If the fix is deployed.": TIMED,
    "It hasn't merged": NEGATED,
    "It never merged.": NEGATED,
    "It will be merged and deployed.": MODAL,
    "It will be merged.": MODAL,
    "Never merged.": NEGATED,
    "No fix merged.": NEGATED,
    "Nobody deployed the fix.": NEGATED,
    "Not everything is deployed.": NEGATED,
    "Nothing merged.": NEGATED,
    "Once the fix is deployed.": TIMED,
    "Tell me when it is merged.": TIMED,
    "The fix isn't deployed.": NEGATED,
    "The fix never merged.": NEGATED,
    "The fix is not merged.": NEGATED,
    "WILL merged.": MODAL,
    "Wait until the fix is deployed.": TIMED,
    "What if the fix is merged?": TIMED,
    "Why wait when the fix is merged?": TIMED,
    "will merged.": MODAL,
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


def corpus_roots() -> dict[str, str]:
    """Each grid sentence with the root sentence it derives from."""
    roots = {sentence: sentence for sentence in (*BASE_SENTENCES, *MUST_BLOCK)}
    roots.update(
        (sentence.rstrip(".?") + suffix, sentence)
        for sentence in BASE_SENTENCES
        for suffix in SUFFIXES
    )
    roots.update(
        (re.sub(r"\b(?:is deployed|merged)\b", replacement, sentence), sentence)
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
    roots.update(
        (f"{subject}'s {status} code into main.", f"{subject}'s {status} code")
        for subject in ("Pat", "Sam", "Alex")
        for status in ("merged", "applied", "deployed")
    )
    roots.update(
        (sentence.rstrip(".") + suffix, sentence)
        for sentence in BASE_SENTENCES[:8]
        for suffix in (
            " at 20 requests per review.",
            " per N workers.",
            " build 12345 failed.",
        )
    )
    return roots


def generated_corpus() -> set[str]:
    return set(corpus_roots())


def main_blocks(sentence: str) -> bool:
    return bool(baseline.unsupported_claims(sentence))


def head_blocks(sentence: str) -> bool:
    return bool(state_guard.unsupported_claims(sentence))


def test_only_intended_simple_sentences_gain_exemptions() -> None:
    assert len(generated_corpus()) >= 300
    assert new_pass_roots() == INTENDED_PASSES.keys()


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


def new_pass_roots() -> set[str]:
    roots = corpus_roots()
    return {roots[s] for s in newly_passing()}


def responsible_rules(sentence: str) -> set[str]:
    """The exemption rules that exempt at least one claim match in SENTENCE."""
    return {
        rule.__name__
        for rule in state_guard.EXEMPTIONS
        for match in state_guard.CLAIM.finditer(sentence)
        if rule(sentence, match)
    }


def test_each_new_pass_comes_from_its_roots_rule() -> None:
    roots = corpus_roots()
    for sentence in newly_passing():
        expected = RULE_NAMES[INTENDED_PASSES[roots[sentence]]]
        assert expected in responsible_rules(sentence), sentence


def exempt_everything(_chunk: str, _match: re.Match[str]) -> bool:
    return True


def test_one_widening_rule_fails_the_differential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert new_pass_roots() == INTENDED_PASSES.keys()
    monkeypatch.setattr(state_guard, "EXEMPTIONS", (exempt_everything,))
    assert new_pass_roots() != INTENDED_PASSES.keys()


def test_head_judges_every_other_grid_sentence_as_main_does() -> None:
    roots = corpus_roots()
    assert all(
        main_blocks(s) == head_blocks(s)
        for s in generated_corpus()
        if roots[s] not in INTENDED_PASSES
    )


def test_the_exemption_set_is_the_documented_one() -> None:
    names = [rule.__name__ for rule in state_guard.EXEMPTIONS]
    assert names == ["timed", "modal", "negated", "quoted"]


# The wrapper grid: each template wraps a status claim in a time, negation, modal,
# quotation or evidence phrase, and each sentence runs in three letter cases. A
# template that main blocks and the head passes must be on ALLOWED_TEMPLATES, so
# every widening is a deliberate line here.
CORES = (
    "the fix merged",
    "it is merged",
    "the PR is deployed",
    "I merged the PR",
    "the build is running",
    "the patch shipped",
    "it is already live",
)
WORDS = ("merged", "deployed", "shipped")
TEMPLATES = (
    "After review {c}.",
    "Once tests pass {c}.",
    "When CI passed {c}.",
    "Until review ends {c}.",
    "If it works {c}.",
    "Before launch {c}.",
    "After review, {c}.",
    "Once tests pass, {c}.",
    "Tell me when {c}.",
    "Wait until {c}.",
    "While we wait {c}.",
    "{c} not long ago.",
    "{c} no thanks to CI.",
    "{c} not just on staging.",
    "{c} not only on staging.",
    "{c} not yesterday but today.",
    "{c} not.",
    "Not long ago {c}.",
    "Not just that, {c}.",
    "No thanks to CI {c}.",
    "{c}, not rejected.",
    "{c} never.",
    "{c} must have.",
    "{c}, which should now be true.",
    "The PR should now be merged.",
    "The fix must have merged.",
    "It will now be merged.",
    "It should by now be deployed.",
    "It should already be deployed.",
    "It will be merged.",
    "It should be merged.",
    'I "{w}" the PR.',
    'The fix is already "{w}".',
    'The fix was just "{w}".',
    'It has also "{w}".',
    'The fix "{w}".',
    'Status: "{w}".',
    'The word "{w}".',
    'We "{w}" it.',
    'Look at "{w}" now.',
    'He "{w}" it.',
    "{c} according to ME.",
    "{c} according to THE PLAN.",
    "{c} according to the plan.",
    "{c} confirmed by the plan.",
    "{c} confirmed by nobody.",
    "{c}, confirmed by Pat.",
    "{c} according to Pat.",
    "{c} confirmed by us.",
)
ALLOWED_TEMPLATES = {
    "Tell me when {c}.",
    "Wait until {c}.",
    "It will be merged.",
    "It should be merged.",
    'Status: "{w}".',
    'The word "{w}".',
    'Look at "{w}" now.',
    "{c}, confirmed by Pat.",
    "{c} according to Pat.",
}


def grid_variants(template: str) -> set[str]:
    sentences = {template.format(c=core, w=word) for core in CORES for word in WORDS}
    sentences = {s[0].upper() + s[1:] for s in sentences}
    return {v for s in sentences for v in (s, s.upper(), s.lower())}


def passing_templates() -> set[str]:
    return {
        t
        for t in TEMPLATES
        if any(main_blocks(s) and not head_blocks(s) for s in grid_variants(t))
    }


def test_the_wrapper_grid_widens_only_through_allowed_templates() -> None:
    assert passing_templates() == ALLOWED_TEMPLATES


def test_a_widened_template_never_passes_in_one_case_only() -> None:
    for template in ALLOWED_TEMPLATES:
        variants = grid_variants(template)
        assert all(main_blocks(s) for s in variants)
