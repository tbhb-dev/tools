import subprocess
from pathlib import Path

import pytest

from tbhbdev import content, cue_runtime

pytestmark = pytest.mark.usefixtures("review_cue")  # noqa: V107

SUMMARY = "## Summary\n\nThe cache preserves the last saved value.\n"
VERIFICATION = "\n## Verification\n\n- Read the cache implementation.\n"
FINDING = "- F1: P1 correctness The cache returns stale values.\n"
DEBT = "- F1: P2 design Rename the fixture. It also serves the CLI.\n"


def body_with(heading: str, text: str) -> str:
    before = "" if heading == "Summary" else SUMMARY + "\n"
    after = "" if heading == "Verification" else VERIFICATION
    return f"{before}## {heading}\n\n{text}\n{after}"


@pytest.mark.parametrize(
    ("heading", "item", "words", "count"),
    [
        ("Summary", "", 40, 1),
        ("Findings", "- F{}: P3 docs ", 30, 8),
        ("Suggestions", "- S{}: ", 25, 5),
        ("Verification", "- ", 20, 6),
    ],
)
def test_review_limits(heading: str, item: str, words: int, count: int) -> None:
    def body(size: int, items: int) -> str:
        prose = "word " * (size - 1) + "word."
        return body_with(
            heading, "\n".join(item.format(n) + prose for n in range(1, items + 1))
        )

    assert not content.check_review(body(words, count))
    assert content.check_review(body(words + 1, 1))
    if heading != "Summary":
        assert content.check_review(body(1, count + 1))


@pytest.mark.parametrize(
    ("heading", "items"),
    [
        ("Findings", "- F1: P0 security Fix it.\n- F3: P2 test Write a test."),
        ("Findings", "- F1: P1 bug Fix it."),
        ("Findings", "- F1: P3 docs Fix it at `src/app.py:4`."),
        ("Findings", "- F1: P2 test **CI** is green."),
        ("Findings", "- F1: P2 test One. Two. Three."),
        ("Suggestions", "- S1: Add an example.\n- S3: Improve the label."),
        ("Suggestions", "- V1: Add an example."),
        ("Verification", "1. Read the cache."),
        ("Verification", "- 1. Read the cache."),
        ("Verification", "- V1: Read the cache."),
        ("Verification", "- [x] Read the cache."),
        ("Verification", "- Read the cache.\n\nA paragraph."),
        ("Verification", "- Read the cache.\nRun: review-build"),
        ("Verification", "- Read the cache.\n- Checked commit a123456."),
    ],
)
def test_review_rejects_invalid_items(heading: str, items: str) -> None:
    assert content.check_review(body_with(heading, items))


@pytest.mark.parametrize(
    ("event", "item", "refused"),
    [
        ("APPROVE", DEBT, False),
        ("APPROVE", FINDING, True),
        ("COMMENT", FINDING, True),
        ("REQUEST_CHANGES", FINDING, False),
        ("REQUEST_CHANGES", FINDING.replace("P1", "P0"), False),
        ("REQUEST_CHANGES", DEBT, True),
        ("REQUEST_CHANGES", "", True),
    ],
)
def test_review_event_matches_severity(event: str, item: str, *, refused: bool) -> None:
    findings = f"\n## Findings\n\n{item}" if item else ""
    problems = content.check_review(SUMMARY + findings + VERIFICATION, event=event)
    assert bool(problems) == refused


def test_check_review_reports_normalization_refusal() -> None:
    assert content.check_review("## <!-- invisible -->\n") == [
        "Section headings must have visible text"
    ]


def test_check_review_reports_missing_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Path:
        message = "run tbhbdev cue install"
        raise cue_runtime.CueError(message)

    monkeypatch.setattr(cue_runtime, "resolve", missing)
    assert content.check_review(SUMMARY + VERIFICATION) == [
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
    assert content.check_review(SUMMARY + VERIFICATION) == ["Review validation failed"]
