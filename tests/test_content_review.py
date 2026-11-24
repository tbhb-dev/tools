import shutil
import subprocess
from pathlib import Path

import pytest

from tbhbdev import content, cue_runtime

SHA = "0123456789abcdef0123456789abcdef01234567"
REVIEW = f"""Run: review-build
Provider: openai
Model: gpt-6
Variant: a
Host: reviewer.example.org
Head: {SHA}
Tier: 1

## Verdict

GO

## Findings

"""
VERIFICATION = (
    "\n## Verification\n\nRead the cache implementation and ran the cache tests.\n"
)
BLOCK = "- BLOCK `src/app.py:4` Return the saved value.\n"
DEBT = "- TECH-DEBT `tests/test_app.py:8` Rename the fixture. It also serves the CLI.\n"
SECURITY = "- SECURITY-DEBT `src/app.py:12` Narrow the file permissions.\n"
LONG_FINDING = "- TECH-DEBT `app.py:1` " + "word " * 39 + "word.\n"
RUN = {
    "name": "review-build",
    "provider": "openai",
    "model": "gpt-6",
    "variant": "a",
    "host": "reviewer.example.org",
}
BLOCK_METADATA = {
    "class": "BLOCK",
    "path": "src/app.py",
    "line": 4,
    "summary": "Return the saved value.",
}


@pytest.fixture
def review_metadata() -> content.Json:
    return {
        "schema": 2,
        "repo": "tbhb-dev/tools",
        "pr": 1,
        "head_sha": SHA,
        "tier": 1,
        "verdict": "GO",
        "run": RUN.copy(),
        "findings": [],
    }


@pytest.fixture
def review_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bazel supplies the same pinned tool on PATH in the test sandbox.
    if executable := shutil.which("cue"):
        monkeypatch.setattr(cue_runtime, "resolve", lambda: Path(executable))


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    "review",
    [
        REVIEW + VERIFICATION,
        REVIEW + DEBT + SECURITY + VERIFICATION,
        REVIEW.replace("Tier: 1", "Tier: 2").replace("\nGO\n", "\nNO-GO\n")
        + BLOCK
        + VERIFICATION,
        REVIEW.replace("Tier: 1", "Tier: 0") + DEBT * 8 + VERIFICATION,
        REVIEW + LONG_FINDING * 5 + VERIFICATION,
        REVIEW + VERIFICATION + "\n" + "word " * 90 + "word.\n",
    ],
)
def test_check_review_accepts(review: str) -> None:
    assert content.check_review(review) == []


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("review", "reason"),
    [
        (REVIEW + BLOCK, "GO forbids BLOCK"),
        (REVIEW.replace("\nGO\n", "\nNO-GO\n") + DEBT, "NO-GO requires"),
        (REVIEW.replace("\nGO\n", "\nHOLD\n"), "blocks.verdict.content"),
        (REVIEW.replace("\nGO\n", "\nAPPROVE\n"), "blocks.verdict.content"),
        (REVIEW.replace("## Verdict", "## Decision"), "blockOrder"),
        (REVIEW + "## Context\n\nMore notes.\n", "blockOrder"),
        ("# Review\n\n" + REVIEW, "blocks.preamble.content"),
        (REVIEW.replace("Provider: openai\n", ""), "blocks.preamble.content"),
        (REVIEW.replace("Run: review-build", "Run:"), "blocks.preamble.content"),
        (REVIEW.replace("Model: gpt-6", "Model:"), "blocks.preamble.content"),
        (
            REVIEW.replace(
                "Provider: openai\nModel: gpt-6", "Model: gpt-6\nProvider: openai"
            ),
            "blocks.preamble.content",
        ),
        (REVIEW.replace("Variant: a", "Variant: c"), "blocks.preamble.content"),
        (
            REVIEW.replace("reviewer.example.org", "Reviewer.example.org"),
            "blocks.preamble.content",
        ),
        (
            REVIEW.replace("reviewer.example.org", "reviewer..org"),
            "blocks.preamble.content",
        ),
        (REVIEW.replace("Tier: 1", "Tier: 3"), "blocks.preamble.content"),
        (REVIEW.replace(SHA, SHA.upper()), "blocks.preamble.content"),
        (REVIEW.replace("Head: 0", "Head: z"), "blocks.preamble.content"),
        (REVIEW.replace("Head: 0", "Head: "), "blocks.preamble.content"),
        (REVIEW.replace("Head: 0", "Head: 00"), "blocks.preamble.content"),
        (REVIEW + DEBT.replace("TECH-DEBT", "NOTE"), "_class"),
        (REVIEW + DEBT.replace(":8", ":0"), "_parts"),
        (REVIEW + DEBT.replace(":8", ""), "_parts"),
        (REVIEW + "1." + DEBT[1:], "blocks.findings.content"),
        (REVIEW + "- [ ] " + DEBT[2:], "task checkbox"),
        (REVIEW + DEBT + "  - Nested item.\n", ".blocks"),
        (REVIEW + "Explain the change.\n", "blocks.findings.content"),
        (REVIEW + "- TECH-DEBT `app.py:1` Missing period\n", "_sentences"),
        (REVIEW + DEBT + "  Keep its old name.\n", "_sentences"),
        (REVIEW + "- TECH-DEBT `app.py:1` **CI** is  green!\n", "CI gate"),
        (REVIEW + "- TECH-DEBT `app.py:1` CI is green .\n", "CI gate"),
        (REVIEW + "- TECH-DEBT `app.py:1` Move the fixture. 12/12 pass.\n", "CI gate"),
        (REVIEW + "- TECH-DEBT `app.py:1` " + "word " * 40 + "word.\n", "_words"),
        (REVIEW + DEBT * 9, "items"),
    ],
)
def test_check_review_refuses(review: str, reason: str) -> None:
    refusals = content.check_review(review + VERIFICATION)
    assert len(refusals) == 1
    assert reason in refusals[0]


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("verification", "reason"),
    [
        ("", "blockOrder"),
        ("\n## Verification\n", "blocks.verification.content"),
        ("\n## Verification\n\n- Read the cache.\n", "blocks.verification.content"),
        (VERIFICATION + "\n" + "word " * 91 + "word.\n", "blocks.verification.metrics"),
    ],
)
def test_check_review_verification(verification: str, reason: str) -> None:
    refusals = content.check_review(REVIEW + verification)
    assert len(refusals) == 1
    assert reason in refusals[0]


@pytest.mark.usefixtures("review_cue")
def test_check_review_accepts_metadata(review_metadata: content.Json) -> None:
    assert content.check_review(REVIEW + VERIFICATION, metadata=review_metadata) == []
    review_metadata.update(
        verdict="NO-GO", tier=0, run={**RUN, "variant": "b"}, findings=[BLOCK_METADATA]
    )
    header = REVIEW.replace("\nGO\n", "\nNO-GO\n").replace("Tier: 1", "Tier: 0")
    header = header.replace("Variant: a", "Variant: b")
    block = BLOCK.replace("saved value", "**saved**\n  value")
    assert (
        content.check_review(header + block + VERIFICATION, metadata=review_metadata)
        == []
    )


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("verdict", "NO-GO", "verdict"),
        ("head_sha", "f" * 40, "head_sha"),
        ("head_sha", SHA.upper(), "head_sha"),
        ("tier", 2, "tier"),
        ("tier", 3, "tier"),
        ("run", {**RUN, "name": "other"}, "run.name"),
        ("run", {**RUN, "provider": "other"}, "run.provider"),
        ("run", {**RUN, "model": "other"}, "run.model"),
        ("run", {**RUN, "variant": "b"}, "run.variant"),
        ("run", {**RUN, "variant": "c"}, "run.variant"),
        ("run", {**RUN, "host": "other.example.org"}, "run.host"),
        ("run", {**RUN, "extra": True}, "run.extra"),
        ("repo", "tools", "repo"),
        ("pr", 0, "pr"),
        ("schema", 1, "schema"),
        ("extra", True, "metadata.extra"),
        ("findings", [BLOCK_METADATA], "findings"),
    ],
)
def test_check_review_refuses_metadata(
    review_metadata: content.Json, field: str, value: object, reason: str
) -> None:
    review_metadata[field] = value
    refusals = content.check_review(REVIEW + VERIFICATION, metadata=review_metadata)
    assert len(refusals) == 1
    assert reason in refusals[0]


@pytest.mark.usefixtures("review_cue")
def test_check_review_requires_supplied_metadata_fields(
    review_metadata: content.Json,
) -> None:
    assert content.check_review(REVIEW + VERIFICATION, metadata={})
    del review_metadata["verdict"]
    assert (
        "metadata.verdict"
        in content.check_review(REVIEW + VERIFICATION, metadata=review_metadata)[0]
    )


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("findings", "reason"),
    [
        ([], "findings"),
        ([{**BLOCK_METADATA, "class": "TECH-DEBT"}], "findings.0.class"),
        ([{**BLOCK_METADATA, "path": "src/other.py"}], "findings.0.path"),
        ([{**BLOCK_METADATA, "line": 5}], "findings.0.line"),
        (
            [{**BLOCK_METADATA, "summary": "Return the cached value."}],
            "findings.0.summary",
        ),
        ([{**BLOCK_METADATA, "summary": "word " * 160 + "word."}], "findings.0._words"),
        ([{**BLOCK_METADATA, "extra": True}], "findings.0.extra"),
    ],
)
def test_check_review_matches_findings(
    review_metadata: content.Json, findings: list[dict[str, object]], reason: str
) -> None:
    review_metadata.update(verdict="NO-GO", findings=findings)
    body = REVIEW.replace("\nGO\n", "\nNO-GO\n") + BLOCK + VERIFICATION
    refusals = content.check_review(body, metadata=review_metadata)
    assert len(refusals) == 1
    assert reason in refusals[0]


def test_check_review_reports_normalization_refusal() -> None:
    assert content.check_review("## <!-- invisible -->\n") == [
        "Section headings must have visible text"
    ]


def test_check_review_reports_missing_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Path:
        message = "run tbhbdev cue install"
        raise cue_runtime.CueError(message)

    monkeypatch.setattr(cue_runtime, "resolve", missing)
    assert content.check_review(REVIEW) == [
        "Review validation could not run: run tbhbdev cue install"
    ]


def test_check_review_reports_silent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def quiet(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(["cue"], 1, stderr="\n")

    monkeypatch.setattr(cue_runtime, "resolve", lambda: Path("cue"))
    monkeypatch.setattr(content.subprocess, "run", quiet)
    assert content.check_review(REVIEW) == ["Review validation failed"]
