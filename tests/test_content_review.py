import shutil
import subprocess
from pathlib import Path

import pytest

from tbhbdev import content, cue_runtime

REVIEW = """Run: review-build
Provider: openai
Model: gpt-6
Head: 0123456789abcdef0123456789ABCDEF01234567
Tier: 1

## Verdict

GO

## Findings

"""
BLOCK = "- BLOCK `src/app.py:4` Return the saved value.\n"
DEBT = "- TECH-DEBT `tests/test_app.py:8` Rename the fixture. It also serves the CLI.\n"
SECURITY = "- SECURITY-DEBT `src/app.py:12` Narrow the file permissions.\n"


@pytest.fixture
def review_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bazel supplies the same pinned tool on PATH in the test sandbox.
    if executable := shutil.which("cue"):
        monkeypatch.setattr(cue_runtime, "resolve", lambda: Path(executable))


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    "review",
    [
        REVIEW,
        REVIEW + DEBT + SECURITY,
        REVIEW.replace("Tier: 1", "Tier: 2").replace("\nGO\n", "\nHOLD\n") + BLOCK,
        REVIEW.replace("Tier: 1", "Tier: 3") + DEBT * 8,
        REVIEW + ("- TECH-DEBT `app.py:1` " + "word " * 39 + "word.\n") * 4,
    ],
)
def test_check_review_accepts(review: str) -> None:
    assert content.check_review(review) == []


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("review", "reason"),
    [
        (REVIEW + BLOCK, "GO forbids BLOCK"),
        (REVIEW.replace("\nGO\n", "\nHOLD\n") + DEBT, "HOLD requires"),
        (REVIEW.replace("\nGO\n", "\nAPPROVE\n"), "blocks.verdict.content"),
        (REVIEW.replace("## Verdict", "## Decision"), "blockOrder"),
        (REVIEW + "## Context\n\nMore notes.\n", "blockOrder"),
        ("# Review\n\n" + REVIEW, "blocks.preamble.content"),
        (REVIEW.replace("Provider: openai\n", ""), "blocks.preamble.content"),
        (REVIEW.replace("Run: review-build", "Run:"), "blocks.preamble.content"),
        (REVIEW.replace("Model: gpt-6", "Model:"), "blocks.preamble.content"),
        (REVIEW.replace("Tier: 1", "Tier: 4"), "blocks.preamble.content"),
        (REVIEW.replace("Head: 0", "Head: z"), "blocks.preamble.content"),
        (REVIEW.replace("Head: 0", "Head: "), "blocks.preamble.content"),
        (REVIEW.replace("Head: 0", "Head: 00"), "blocks.preamble.content"),
        (REVIEW + DEBT.replace("TECH-DEBT", "NOTE"), "_parts"),
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
        (
            REVIEW + ("- TECH-DEBT `app.py:1` " + "word " * 39 + "word.\n") * 4 + DEBT,
            "_totalWords",
        ),
    ],
)
def test_check_review_refuses(review: str, reason: str) -> None:
    refusals = content.check_review(review)
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
    def quiet(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["cue"], 1, stderr="\n")

    monkeypatch.setattr(cue_runtime, "resolve", lambda: Path("cue"))
    monkeypatch.setattr(content.subprocess, "run", quiet)
    assert content.check_review(REVIEW) == ["Review validation failed"]
