import json
import shutil
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast

import pytest

from tbhbdev import content, cue_runtime
from tbhbdev.alpha import review_doc
from tbhbdev.alpha.review_doc import Existing, Finding, Marker, ReviewDoc, Run

HEAD = "0123456789abcdef0123456789abcdef01234567"
FRONTMATTER = "---\nschema: 2\nrepo: tbhb-dev/tools\npr: 384\n---\n"
RUN = Run("review-build", "openai", "gpt-6", "a", "reviewer.example.org")
BLOCK = Finding("BLOCK", "src/app.py", 4, "Return the saved value.")
DEBT = Finding("TECH-DEBT", "tests/test_app.py", 8, "Rename the fixture.")
SECURITY = Finding("SECURITY-DEBT", "src/app.py", 12, "Narrow the file permissions.")
DOC = ReviewDoc(
    "tbhb-dev/tools",
    384,
    HEAD,
    2,
    "NO-GO",
    RUN,
    (BLOCK, DEBT, SECURITY),
    "Ran the cache tests.\n\nRead the cache implementation.",
)
BODY = f"""Run: review-build
Provider: openai
Model: gpt-6
Variant: a
Host: reviewer.example.org
Head: {HEAD}
Tier: 2

## Verdict

NO-GO

## Findings

- BLOCK `src/app.py:4` Return the saved value.
- TECH-DEBT `tests/test_app.py:8` Rename the fixture.
- SECURITY-DEBT `src/app.py:12` Narrow the file permissions.

## Verification

Ran the cache tests.

Read the cache implementation.
"""
MARKER = (
    f"<!-- tbhb-review v1 repo=tbhb-dev/tools pr=384 head={HEAD} run=review-build -->"
)
KEY = Marker("tbhb-dev/tools", 384, HEAD, "review-build")
EXISTING = Existing(942, "reviews[bot]", "COMMENTED", f"{BODY}\n{MARKER}\n")
METADATA = {
    "schema": 2,
    "repo": "tbhb-dev/tools",
    "pr": 384,
    "head_sha": HEAD,
    "tier": 2,
    "verdict": "NO-GO",
    "run": {
        "name": "review-build",
        "provider": "openai",
        "model": "gpt-6",
        "variant": "a",
        "host": "reviewer.example.org",
    },
    "findings": [
        {
            "class": "BLOCK",
            "path": "src/app.py",
            "line": 4,
            "summary": "Return the saved value.",
        },
        {
            "class": "TECH-DEBT",
            "path": "tests/test_app.py",
            "line": 8,
            "summary": "Rename the fixture.",
        },
        {
            "class": "SECURITY-DEBT",
            "path": "src/app.py",
            "line": 12,
            "summary": "Narrow the file permissions.",
        },
    ],
}


@pytest.fixture
def review_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bazel puts the pinned CUE executable on PATH inside its test sandbox.
    if executable := shutil.which("cue"):
        monkeypatch.setattr(cue_runtime, "resolve", lambda: Path(executable))


@pytest.mark.parametrize(
    ("instance", "field"),
    [
        (RUN, "name"),
        (BLOCK, "class_"),
        (DOC, "verdict"),
        (KEY, "head"),
        (EXISTING, "id"),
    ],
)
def test_review_records_are_immutable(instance: object, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(instance, field, None)


def test_render_and_parse() -> None:
    rendered = review_doc.render(DOC)
    assert rendered == f"{BODY}\n{MARKER}\n"
    assert review_doc.parse(FRONTMATTER + rendered.removesuffix(f"\n{MARKER}\n")) == DOC


def test_render_empty_findings_and_alternate_run() -> None:
    doc = replace(
        DOC,
        tier=0,
        verdict="GO",
        findings=(),
        run=replace(RUN, variant="b"),
        verification="Read the cache.\n\n",
    )
    rendered = review_doc.render(doc)
    assert rendered.endswith(
        f"\n## Findings\n\n## Verification\n\nRead the cache.\n\n{MARKER}\n"
    )
    body = rendered.removesuffix(f"\n{MARKER}\n")
    assert review_doc.parse(FRONTMATTER + body) == replace(
        doc, verification="Read the cache."
    )


def test_parse_matching_frontmatter() -> None:
    frontmatter = f"---\n{json.dumps(METADATA)}\n---\n\n"
    assert review_doc.parse((frontmatter + BODY).replace("\n", "\r\n")) == DOC


@pytest.mark.parametrize(
    ("draft", "problem"),
    [
        (BODY, "^expected schema 2 frontmatter with repo and integer pr$"),
        ("---\nschema: 2\n" + BODY, "unterminated frontmatter"),
        (FRONTMATTER.replace("pr: 384", "pr: [") + BODY, "invalid YAML"),
        (
            FRONTMATTER.replace("pr: 384", "pr: 384\npr: 383") + BODY,
            "duplicate key",
        ),
        (FRONTMATTER.replace("schema: 2", "schema: 1") + BODY, "expected schema 2"),
        (FRONTMATTER.replace("pr: 384", "pr: true") + BODY, "integer pr"),
        (
            FRONTMATTER.replace("pr: 384", "pr: 384\nextra: value") + BODY,
            "field: extra",
        ),
        (FRONTMATTER.replace("pr: 384", "pr: 384\ntier: 1") + BODY, "field: tier"),
        (
            FRONTMATTER + BODY.replace("## Verdict", "## Verification", 1),
            "^expected Verdict, Findings, and Verification sections in order$",
        ),
        (
            FRONTMATTER + BODY.replace("Tier: 2", "Tier: 3"),
            "^expected the seven run header lines$",
        ),
        (FRONTMATTER + " " + BODY, "seven run header lines"),
        (FRONTMATTER + "X" + BODY, "seven run header lines"),
        (
            FRONTMATTER + BODY.replace("\nNO-GO\n", "\nHOLD\n"),
            "^expected a GO or NO-GO verdict$",
        ),
        (FRONTMATTER + BODY.replace("- BLOCK", "- NOTE"), "invalid finding: '- NOTE"),
        (FRONTMATTER + BODY.replace("app.py:4", "app.py:0"), "invalid finding"),
    ],
)
def test_parse_refuses_malformed_drafts(draft: str, problem: str) -> None:
    with pytest.raises(review_doc.ReviewError, match=problem):
        review_doc.parse(draft)


@pytest.mark.parametrize(
    ("summary", "verification"),
    [("Handle X", "Xcode uses API X"), ("Handle X  ", "Read the output.  ")],
)
def test_round_trip_preserves_verification_and_summary(
    summary: str, verification: str
) -> None:
    # The parser preserves the source text for the schema validator.
    doc = replace(
        DOC, findings=(replace(BLOCK, summary=summary),), verification=verification
    )
    rendered = review_doc.render(doc)
    assert rendered.endswith(f"\n{verification}\n\n{MARKER}\n")
    body = rendered.removesuffix(f"\n{MARKER}\n")
    assert review_doc.parse(FRONTMATTER + body) == doc


def test_marker_round_trip() -> None:
    assert review_doc.marker(DOC) == MARKER
    assert review_doc.parse_marker(review_doc.render(DOC)) == KEY
    assert review_doc.marker(replace(DOC, run=replace(RUN, variant="b"))) == MARKER


def test_parse_marker_uses_last_matching_line() -> None:
    wrong = MARKER.replace("pr=384", "pr=12")
    older = MARKER.replace("run=review-build", "run=other")
    invalid = MARKER.replace(HEAD, HEAD.upper())
    assert review_doc.parse_marker(f"{wrong}\n{MARKER}\n{invalid}") == KEY
    assert review_doc.parse_marker(f"{wrong}\n{older}\n{MARKER}") == KEY


@pytest.mark.parametrize(
    "body",
    [
        "No marker.",
        f"Review {MARKER}",
        f"{MARKER} review",
        f" {MARKER}",
        f"{MARKER} ",
        MARKER.replace(HEAD, HEAD.upper()),
        MARKER.replace("v1 ", "v2 "),
        MARKER.replace("run=review-build", "run=review build"),
    ],
)
def test_parse_marker_refuses_spoofs(body: str) -> None:
    assert review_doc.parse_marker(body) is None


@pytest.mark.parametrize(
    ("state", "body", "expected"),
    [
        ("DISMISSED", BODY, True),
        ("COMMENTED", "SUPERSEDED: stale head\n" + BODY, True),
        ("COMMENTED", "Older SUPERSEDED: text", False),
        ("dismissed", BODY, False),
    ],
)
def test_is_dismissed(state: str, body: str, expected: bool) -> None:
    assert (
        review_doc.is_dismissed(replace(EXISTING, state=state, body=body)) is expected
    )


def test_validate_passes_exact_body_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def check_review(markdown: str, *, metadata: content.Json) -> list[str]:
        assert markdown == BODY
        assert metadata == METADATA
        return ["CUE refusal"]

    monkeypatch.setattr(content, "check_review", check_review)
    assert review_doc.validate(DOC) == ["CUE refusal"]


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        (DOC, []),
        (replace(DOC, verdict="GO", findings=(DEBT,)), []),
        (
            replace(DOC, head_sha=HEAD.upper()),
            ["head_sha must be 40 lowercase hexadecimal characters"],
        ),
        (
            replace(DOC, head_sha=HEAD + "\n"),
            ["head_sha must be 40 lowercase hexadecimal characters"],
        ),
        (replace(DOC, verdict="GO"), ["GO forbids BLOCK findings"]),
        (replace(DOC, findings=(DEBT,)), ["NO-GO requires at least one BLOCK finding"]),
    ],
)
def test_validate_cross_checks(
    monkeypatch: pytest.MonkeyPatch, doc: ReviewDoc, expected: list[str]
) -> None:
    def accept(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(content, "check_review", accept)
    assert review_doc.validate(doc) == expected


def test_validate_reports_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> list[str]:
        message = "test validator failed"
        raise RuntimeError(message)

    monkeypatch.setattr(content, "check_review", fail)
    assert review_doc.validate(replace(DOC, verdict="GO")) == [
        "GO forbids BLOCK findings",
        "Review validation could not run: test validator failed",
    ]


@pytest.mark.usefixtures("review_cue")
def test_validate_accepts_eight_findings_and_over_160_words_in_total() -> None:
    finding = replace(DEBT, summary="word " * 39 + "word.")
    assert review_doc.validate(DOC) == []
    assert (
        review_doc.validate(replace(DOC, verdict="GO", findings=(finding,) * 8)) == []
    )


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("doc", "problem"),
    [
        (replace(DOC, repo="tools"), "metadata.repo"),
        (
            replace(DOC, run=replace(RUN, host="Reviewer.example.org")),
            "metadata.run.host",
        ),
        (replace(DOC, findings=(BLOCK,) * 9), "findings"),
        (replace(DOC, findings=(replace(BLOCK, line=0),)), "findings"),
        (
            replace(DOC, findings=(replace(BLOCK, summary="word " * 40 + "word."),)),
            "_words",
        ),
        (
            replace(DOC, findings=(replace(BLOCK, summary="word " * 160 + "word."),)),
            "metadata.findings.0._words",
        ),
        (replace(DOC, verification=""), "verification"),
        (replace(DOC, verification="word " * 100 + "word."), "verification"),
    ],
)
def test_validate_returns_cue_refusals(doc: ReviewDoc, problem: str) -> None:
    assert any(problem in error for error in review_doc.validate(doc))


def test_api_stubs_raise() -> None:
    api = cast("review_doc.Api", object())
    with pytest.raises(
        NotImplementedError, match=r"^review lookup is not implemented$"
    ):
        review_doc.find_existing(api, KEY, author=EXISTING.author)
    with pytest.raises(
        NotImplementedError, match=r"^review posting is not implemented$"
    ):
        review_doc.post(api, DOC, author=EXISTING.author)
    with pytest.raises(
        NotImplementedError, match=r"^review dismissal is not implemented$"
    ):
        review_doc.dismiss(api, DOC.repo, DOC.pr, EXISTING, "stale head")
    with pytest.raises(
        NotImplementedError, match=r"^review replies are not implemented$"
    ):
        review_doc.reply(api, DOC.repo, DOC.pr, EXISTING.id, HEAD, "Retested.")
