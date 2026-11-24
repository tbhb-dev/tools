import json
from dataclasses import asdict, replace

import pytest

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
EXISTING = Existing(942, "reviews[bot]", KEY, "COMMENTED", f"{BODY}\n{MARKER}\n")
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
        ("384\n---", "384", "invalid review frontmatter"),
        ("pr: 384", "pr: [", "invalid review frontmatter"),
        ("pr: 384", "pr: 384\npr: 383", "invalid review frontmatter"),
        ("pr: 384", 'pr: !!int ""', "invalid review frontmatter"),
        ("pr: 384", "pr: !!bool maybe", "invalid review frontmatter"),
        ("pr: 384", f"pr: !!float {'1:' * 200}1", "invalid review frontmatter"),
        ("schema: 2", "schema: 1", "expected schema 2"),
        ("pr: 384", "pr: true", "integer pr"),
        ("pr: 384", "pr: 384\nextra: value", "mismatched review frontmatter field"),
        ("pr: 384", "pr: 384\ntier: 1", "mismatched review frontmatter field"),
        (
            "Verdict\n",
            "Findings\n",
            r"^expected Verdict, Findings, and Verification sections in order$",
        ),
        ("Tier: 2", "Tier: 3", r"^expected the seven run header lines$"),
        ("Run:", " Run:", "seven run header lines"),
        ("Run:", "XRun:", "seven run header lines"),
        ("NO-GO", "HOLD", r"^expected a GO or NO-GO verdict$"),
        ("- BLOCK", "- NOTE", "invalid finding"),
        ("app.py:4", "app.py:0", "invalid finding"),
    ],
)
def test_parse_refuses_malformed_drafts(old: str, new: str, problem: str) -> None:
    with pytest.raises(review.ReviewError, match=problem):
        review.parse((PREAMBLE + BODY).replace(old, new, 1))


def test_parse_refuses_deeply_nested_yaml() -> None:
    nested = "[" * 2000 + "0" + "]" * 2000
    draft = (PREAMBLE + BODY).replace("pr: 384", f"pr: {nested}", 1)
    with pytest.raises(review.ReviewError, match=r"^invalid review frontmatter$"):
        review.parse(draft)


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("pr: 384", "private: [", "invalid review frontmatter"),
        (
            "pr: 384",
            "pr: 384\nprivate: draft",
            "unexpected or mismatched review frontmatter field",
        ),
        ("- BLOCK", "- private", "invalid finding"),
    ],
)
def test_parse_errors_omit_draft_text(old: str, new: str, problem: str) -> None:
    with pytest.raises(review.ReviewError) as refused:
        review.parse((PREAMBLE + BODY).replace(old, new, 1))
    assert str(refused.value) == problem
    assert refused.value.__suppress_context__ or refused.value.__context__ is None


def test_parse_refuses_oversized_finding_line() -> None:
    draft = (PREAMBLE + BODY).replace("app.py:4", f"app.py:{'9' * 5000}", 1)
    with pytest.raises(review.ReviewError, match=r"^invalid finding line number$"):
        review.parse(draft)


@pytest.mark.parametrize("spacer", ["", "  "])
def test_round_trip_preserves_summary_and_verification(spacer: str) -> None:
    finding = replace(DEBT, class_="SECURITY-DEBT", summary=f"Handle X{spacer}")
    verification = f"Read cache tests.\n\nChecked API X{spacer}"
    doc = replace(DOC, verdict="GO", findings=(finding,), verification=verification)
    assert (
        review.parse(PREAMBLE + review.render(doc).removesuffix(f"\n{MARKER}\n")) == doc
    )


def test_marker_uses_last_matching_line() -> None:
    assert review.marker(DOC) == KEY
    assert review.marker(replace(DOC, run=replace(RUN, variant="b"))) == KEY
    assert KEY.head_sha == HEAD
    assert review.parse_marker(review.render(DOC)) == KEY
    wrong = MARKER.replace("pr=384", "pr=12")
    older = MARKER.replace("run=review-build", "run=other")
    invalid = MARKER.replace(HEAD, HEAD.upper())
    assert review.parse_marker(f"{wrong}\n{MARKER}\n{invalid}") == KEY
    assert review.parse_marker(f"{wrong}\n{older}\n{MARKER}") == KEY


def test_parse_marker_accepts_crlf_and_maximum_pr_length() -> None:
    number = 999_999_999_999
    marker = MARKER.replace("pr=384", f"pr={number}")
    assert review.parse_marker(f"{MARKER}\r\n{marker}\r\n") == replace(KEY, pr=number)


@pytest.mark.parametrize(
    "body",
    [
        f"Review {MARKER}",
        f"{MARKER} review",
        f" {MARKER}",
        f"{MARKER} ",
        MARKER.replace(HEAD, HEAD.upper()),
        MARKER.replace("pr=384", f"pr={'9' * 5000}"),
        MARKER.replace("pr=384", "pr=1000000000000"),
        MARKER.replace("pr=384", "pr=\u066384"),
        MARKER.replace("pr=384", "pr=00384"),
        MARKER.replace("pr=384", "pr=0"),
        MARKER.replace("review-build", "review\xa0build"),
        MARKER.replace(REPO, "tbhb-dev/\xa0tools"),
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
