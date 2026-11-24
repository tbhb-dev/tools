import json
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest

from tbhbdev import content, cue_runtime
from tbhbdev.alpha import review_doc as review
from tbhbdev.alpha.review_doc import Existing, Finding, Marker, ReviewDoc, Run

HEAD = "0123456789abcdef0123456789abcdef01234567"
REPO = "tbhb-dev/tools"
RUN = Run("review-build", "openai", "gpt-6", "a", "reviewer.example.org")
BLOCK = Finding("BLOCK", "src/app.py", 4, "Return the saved value.")
DEBT = Finding("TECH-DEBT", "tests/test_app.py", 8, "Rename the fixture.")
DOC = ReviewDoc(REPO, 384, HEAD, 2, "NO-GO", RUN, (BLOCK,), "Read the cache.")
PREAMBLE = "---\nschema: 2\nrepo: tbhb-dev/tools\npr: 384\n---\n"
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

## Verification

Read the cache.
"""
MARKER = f"<!-- tbhb-review v1 repo={REPO} pr=384 head={HEAD} run=review-build -->"
KEY = Marker(REPO, 384, HEAD, "review-build")
EXISTING = Existing(942, "reviews[bot]", "COMMENTED", f"{BODY}\n{MARKER}\n")
METADATA = {
    "schema": 2,
    "repo": REPO,
    "pr": 384,
    "head_sha": HEAD,
    "tier": 2,
    "verdict": "NO-GO",
    "run": asdict(RUN),
    "findings": [
        {"class": "BLOCK", "path": "src/app.py", "line": 4, "summary": BLOCK.summary}
    ],
}


def test_render_and_parse() -> None:
    rendered = review.render(DOC)
    assert rendered == f"{BODY}\n{MARKER}\n"
    body = rendered.removesuffix(f"\n{MARKER}\n")
    assert review.parse(PREAMBLE + body) == DOC
    full = f"---\n{json.dumps(METADATA)}\n---\n\n{body}"
    assert review.parse(full.replace("\n", "\r\n")) == DOC


def test_render_empty_findings_and_alternate_run() -> None:
    run = replace(RUN, variant="b")
    doc = replace(
        DOC, verdict="GO", findings=(), run=run, verification="Read the cache.\n\n"
    )
    rendered = review.render(doc)
    ending = f"## Findings\n\n## Verification\n\nRead the cache.\n\n{MARKER}\n"
    assert rendered.endswith(ending)
    body = rendered.removesuffix(f"\n{MARKER}\n")
    assert review.parse(PREAMBLE + body) == replace(doc, verification="Read the cache.")


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (PREAMBLE, "", r"^expected schema 2 frontmatter with repo and integer pr$"),
        ("384\n---", "384", "unterminated frontmatter"),
        ("pr: 384", "pr: [", "invalid YAML"),
        ("pr: 384", "pr: 384\npr: 383", "duplicate key"),
        ("schema: 2", "schema: 1", "expected schema 2"),
        ("pr: 384", "pr: true", "integer pr"),
        ("pr: 384", "pr: 384\nextra: value", "field: extra"),
        ("pr: 384", "pr: 384\ntier: 1", "field: tier"),
        (
            "Verdict\n",
            "Findings\n",
            r"^expected Verdict, Findings, and Verification sections in order$",
        ),
        ("Tier: 2", "Tier: 3", r"^expected the seven run header lines$"),
        ("Run:", " Run:", "seven run header lines"),
        ("Run:", "XRun:", "seven run header lines"),
        ("NO-GO", "HOLD", r"^expected a GO or NO-GO verdict$"),
        ("- BLOCK", "- NOTE", "invalid finding: '- NOTE"),
        ("app.py:4", "app.py:0", "invalid finding"),
    ],
)
def test_parse_refuses_malformed_drafts(old: str, new: str, problem: str) -> None:
    with pytest.raises(review.ReviewError, match=problem):
        review.parse((PREAMBLE + BODY).replace(old, new, 1))


@pytest.mark.parametrize("spacer", ["", "  "])
def test_round_trip_preserves_summary_and_verification(spacer: str) -> None:
    finding = replace(DEBT, class_="SECURITY-DEBT", summary=f"Handle X{spacer}")
    verification = f"Read cache tests.\n\nChecked API X{spacer}"
    doc = replace(DOC, verdict="GO", findings=(finding,), verification=verification)
    assert (
        review.parse(PREAMBLE + review.render(doc).removesuffix(f"\n{MARKER}\n")) == doc
    )


def test_marker_uses_last_matching_line() -> None:
    assert review.marker(DOC) == MARKER
    assert review.parse_marker(review.render(DOC)) == KEY
    wrong = MARKER.replace("pr=384", "pr=12")
    older = MARKER.replace("run=review-build", "run=other")
    invalid = MARKER.replace(HEAD, HEAD.upper())
    assert review.parse_marker(f"{wrong}\n{MARKER}\n{invalid}") == KEY
    assert review.parse_marker(f"{wrong}\n{older}\n{MARKER}") == KEY


@pytest.mark.parametrize(
    "body",
    [
        f"Review {MARKER}",
        f"{MARKER} review",
        f" {MARKER}",
        f"{MARKER} ",
        MARKER.replace(HEAD, HEAD.upper()),
    ],
)
def test_parse_marker_refuses_spoofs(body: str) -> None:
    assert review.parse_marker(body) is None


def test_is_dismissed() -> None:
    assert review.is_dismissed(replace(EXISTING, state="DISMISSED"))
    assert review.is_dismissed(
        replace(EXISTING, body="SUPERSEDED: stale head\n" + BODY)
    )
    assert not review.is_dismissed(
        replace(EXISTING, state="dismissed", body="Older SUPERSEDED: text")
    )


def test_validate_passes_exact_body_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def check_review(markdown: str, *, metadata: content.Json) -> list[str]:
        assert (markdown, metadata) == (BODY, METADATA)
        return ["CUE refusal"]

    monkeypatch.setattr(content, "check_review", check_review)
    assert review.validate(DOC) == ["CUE refusal"]


def test_validate_cross_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    def accept(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(content, "check_review", accept)
    assert review.validate(DOC) == []
    assert review.validate(replace(DOC, verdict="GO", findings=(DEBT,))) == []
    for head in (HEAD.upper(), HEAD + "\n"):
        assert review.validate(replace(DOC, head_sha=head)) == [
            "head_sha must be 40 lowercase hexadecimal characters"
        ]
    assert review.validate(replace(DOC, verdict="GO")) == ["GO forbids BLOCK findings"]
    assert review.validate(replace(DOC, findings=(DEBT,))) == [
        "NO-GO requires at least one BLOCK finding"
    ]


def test_validate_reports_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> list[str]:
        message = "test validator failed"
        raise RuntimeError(message)

    monkeypatch.setattr(content, "check_review", fail)
    assert review.validate(replace(DOC, verdict="GO")) == [
        "GO forbids BLOCK findings",
        "Review validation could not run: test validator failed",
    ]


def test_validate_schema_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bazel puts the pinned CUE executable on PATH inside its test sandbox.
    if executable := shutil.which("cue"):
        monkeypatch.setattr(cue_runtime, "resolve", lambda: Path(executable))
    doc = replace(DOC, verdict="GO")
    long = replace(DEBT, summary="word " * 39 + "word.")
    assert review.validate(DOC) == []
    assert review.validate(replace(doc, findings=(long,) * 8)) == []
    for invalid, problem in (
        (replace(DOC, repo="tools"), "metadata.repo"),
        (replace(DOC, findings=(BLOCK,) * 9), "findings"),
        (
            replace(doc, findings=(replace(DEBT, summary="word " * 40 + "word."),)),
            "_words",
        ),
        (
            replace(doc, findings=(replace(DEBT, summary="word " * 160 + "word."),)),
            "metadata.findings.0._words",
        ),
        (replace(DOC, verification=""), "verification"),
    ):
        assert any(problem in error for error in review.validate(invalid))


def test_api_stubs_raise() -> None:
    api = cast("review.Api", object())
    with pytest.raises(NotImplementedError, match=r"^review lookup pending$"):
        review.find_existing(api, KEY, author=EXISTING.author)
    with pytest.raises(NotImplementedError, match=r"^review posting pending$"):
        review.post(api, DOC, author=EXISTING.author)
    with pytest.raises(NotImplementedError, match=r"^review dismissal pending$"):
        review.dismiss(api, REPO, DOC.pr, EXISTING, "stale head")
    with pytest.raises(NotImplementedError, match=r"^review replies pending$"):
        review.reply(api, REPO, DOC.pr, EXISTING.id, HEAD, "Retested.")
