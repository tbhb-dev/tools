import json
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from tbhbdev import content, cue_runtime
from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import review as cli
from tbhbdev.alpha import review_doc as review
from tbhbdev.alpha.review_doc import Existing, Finding, Marker, ReviewDoc, Run
from tbhbdev.alpha.review_doc import _metadata as review_metadata

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
IDENTITY = "LLM harness=codex;model=openai:gpt-6;session=sha-256-64:0123456789abcdef"
KEY = Marker(REPO, 384, HEAD, "review-build")
EXISTING = Existing(942, "reviews[bot]", KEY, "COMMENTED", f"{BODY}\n{MARKER}\n")
PULL = f"repos/{REPO}/pulls/384"
REVIEWS = f"{PULL}/reviews"
PAGE = f"{REVIEWS}?per_page=100&page="
LIVE = {"head": {"sha": HEAD}}
USER = {"login": EXISTING.author}
ROW = {"id": 942, "user": USER, "state": "COMMENTED", "body": MARKER}
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


def test_metadata_matches_frontmatter() -> None:
    assert review_metadata(DOC) == METADATA


def test_render_and_parse_multiple_findings() -> None:
    doc = replace(DOC, findings=(BLOCK, DEBT))
    body = review.render(doc).removesuffix(f"\n{MARKER}\n")
    assert review.parse(PREAMBLE + body) == doc


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


@pytest.fixture
def use_bazel_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bazel puts the pinned CUE executable on PATH inside its test sandbox.
    if executable := shutil.which("cue"):
        monkeypatch.setattr(cue_runtime, "resolve", lambda: Path(executable))


@pytest.mark.usefixtures("use_bazel_cue")
def test_validate_uses_visible_finding_summary() -> None:
    summary = (
        "Call  `find_existing` via [the lookup](https://example.org)\u2003*first*."
    )
    doc = replace(DOC, findings=(replace(BLOCK, summary=summary),))
    metadata = METADATA | {
        "findings": [
            {"class": "BLOCK", "path": "src/app.py", "line": 4, "summary": summary}
        ]
    }
    body = review.render(doc).removesuffix(f"\n{MARKER}\n")
    draft = f"---\n{json.dumps(metadata)}\n---\n{body}"
    parsed = review.parse(draft)
    assert parsed == doc
    assert content.check_review(body) == []
    assert review.validate(parsed) == []


@pytest.mark.usefixtures("use_bazel_cue")
def test_validate_schema_and_caps() -> None:
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


class FakeApi:
    def __init__(self, *responses: object) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object:
        self.calls.append((method, path, payload))
        return next(self.responses)


def test_find_existing_uses_latest_active_match_across_pages() -> None:
    ignored = [
        ROW | {"user": {"login": "other"}},
        ROW | {"body": ""},
        ROW | {"body": f"quoted {MARKER}"},
        ROW | {"body": MARKER.replace("pr=384", "pr=385")},
        ROW | {"state": "DISMISSED"},
        ROW | {"body": f"SUPERSEDED: reason\n{MARKER}"},
    ]
    first = [ROW, *ignored, *([None] * 93)]
    api = FakeApi(first, [ROW | {"id": 22}, ROW | {"id": 23}, *ignored])
    assert review.find_existing(api, REPO, 384, HEAD, KEY.run, "Reviews[bot]") == 23
    assert api.calls == [("GET", PAGE + "1", None), ("GET", PAGE + "2", None)]


def test_find_existing_refuses_non_list() -> None:
    with pytest.raises(review.ReviewError, match=r"^expected a list of reviews$"):
        review.find_existing(FakeApi({}), REPO, 384, HEAD, KEY.run, EXISTING.author)


@pytest.mark.usefixtures("use_bazel_cue")
def test_post_refuses_moved_head_and_invalid_doc() -> None:
    api = FakeApi({"head": {"sha": "f" * 40}})
    with pytest.raises(review.ReviewError, match=r"^pull request head has moved$"):
        review.post(api, DOC, author=EXISTING.author, identity_value=IDENTITY)
    with pytest.raises(review.ReviewError, match="GO forbids BLOCK findings"):
        review.post(
            api,
            replace(DOC, verdict="GO"),
            author=EXISTING.author,
            identity_value=IDENTITY,
        )
    assert api.calls == [("GET", PULL, None)]


@pytest.fixture
def draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    stem = tmp_path / "review-384"
    monkeypatch.setattr(cli, "draft_path", {384: stem.with_suffix(".md")}.__getitem__)
    monkeypatch.setattr(cli.pr, "origin_repository", lambda: REPO)
    monkeypatch.setattr(cli.pr, "posting_value", Mock(return_value=IDENTITY))
    return stem.with_suffix(".doc.md")


@pytest.mark.usefixtures("use_bazel_cue")
@pytest.mark.parametrize(
    ("doc", "response", "expected"),
    [
        (DOC, {"id": 16}, 16),
        (replace(DOC, verdict="GO", findings=(DEBT,)), {}, None),
        (replace(DOC, verdict="GO", findings=()), None, None),
    ],
)
def test_post_and_cli_only_send_comments(  # noqa: PLR0913, PLR0917
    doc: ReviewDoc,
    response: object,
    expected: int | None,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = review.render(doc)
    assert agent_identity.check_value(IDENTITY) is None
    posted_body = f"{body}\nGenerated-by: {IDENTITY}\n"
    assert review.parse_marker(posted_body) == KEY
    calls = [
        ("GET", PULL, None),
        ("GET", PAGE + "1", None),
        ("POST", REVIEWS, {"commit_id": HEAD, "body": posted_body, "event": "COMMENT"}),
    ]
    api = FakeApi(LIVE, [], response)
    assert review.post(api, doc, author=EXISTING.author, identity_value=IDENTITY) == (
        "posted",
        expected,
    )
    assert api.calls == calls
    api = FakeApi(USER, LIVE, [], response)
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    draft.write_text(PREAMBLE + body.removesuffix(f"\n{MARKER}\n"))
    assert cli.main(["post", "384"], "review") == 0
    assert api.calls == [("GET", "user", None), *calls]
    output = "posted\n" if expected is None else f"posted {expected}\n"
    assert capsys.readouterr() == (output, "")


@pytest.mark.usefixtures("use_bazel_cue")
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "posting a review requires a recognized agent session"),
        ("LLM harness=codex", "invalid LLM trailer: model required"),
    ],
)
def test_post_cli_refuses_an_unavailable_or_invalid_identity(
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
    message: str,
) -> None:
    draft.write_text(PREAMBLE + BODY)
    api = FakeApi(USER, LIVE, [])
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    monkeypatch.setattr(cli.pr, "posting_value", Mock(return_value=value))
    assert cli.main(["post", "384"], "review") == 1
    assert capsys.readouterr() == ("", f"{draft}: {message}\n")
    assert api.calls == [
        ("GET", "user", None),
        ("GET", PULL, None),
        ("GET", PAGE + "1", None),
    ]


@pytest.mark.usefixtures("use_bazel_cue")
def test_post_cli_exists_and_refusals(
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeApi(LIVE, [ROW], {})
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    args = ["post", "384", "--author", EXISTING.author]
    assert cli.main(args, "review") == 1
    assert capsys.readouterr().err.startswith(f"{draft}: ")
    draft.write_text(PREAMBLE + BODY)
    assert cli.main(args, "review") == 0
    assert capsys.readouterr() == ("exists 942\n", "")
    assert api.calls == [("GET", PULL, None), ("GET", PAGE + "1", None)]
    assert cli.main(["post", "384"], "review") == 1
    assert capsys.readouterr() == ("", f"{draft}: GitHub did not return a login\n")
    draft.write_text((PREAMBLE + BODY).replace("pr: 384", "pr: 383"))
    assert cli.main(args, "review") == 1
    assert capsys.readouterr() == ("", f"{draft}: draft must name pull request 384\n")
    draft.write_text((PREAMBLE + BODY).replace("NO-GO", "GO"))
    assert cli.main(args, "review") == 1
    err = capsys.readouterr().err
    assert err.startswith(f"{draft}: GO forbids BLOCK findings\n")
    assert all(line.startswith(f"{draft}: ") for line in err.splitlines())


@pytest.mark.usefixtures("use_bazel_cue")
def test_post_cli_refuses_wrong_repository(
    draft: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "other/tools"
    draft.write_text(PREAMBLE.replace(REPO, other) + BODY)
    api = FakeApi(USER, LIVE, [], {"id": 16})
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    assert cli.main(["post", "384"], "review") == 1
    message = f"draft repo {other} differs from origin repo {REPO}"
    assert capsys.readouterr() == ("", f"{draft}: {message}\n")
    assert api.calls == []


@pytest.mark.parametrize(
    ("code", "stdout", "error", "payload"),
    [
        (0, '{"id": 16}', "", {"event": "COMMENT"}),
        (0, "", "", None),
        (0, "invalid", "GitHub returned invalid JSON", None),
        (-1, '{"id": 16}', "request failed", None),
    ],
)
def test_gh_api(
    code: int,
    stdout: str,
    error: str,
    payload: dict[str, object] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(args: list[str], *, stdin: str) -> CompletedProcess[str]:
        suffix = ["--input", "-"] if payload is not None else []
        assert args == ["/fake/gh", "api", "--method", "POST", REVIEWS, *suffix]
        assert stdin == (json.dumps(payload) if payload is not None else "")
        return CompletedProcess(args, code, stdout, error + "\n")

    monkeypatch.setattr(cli.pr, "find_tool", {cli.pr.GH: "/fake/gh"}.__getitem__)
    monkeypatch.setattr(cli.pr, "run", run)
    if error:
        with pytest.raises(review.ReviewError, match=f"^{error}$"):
            cli.GhApi().request("POST", REVIEWS, payload)
    else:
        data = cli.GhApi().request("POST", REVIEWS, payload)
        assert data == json.loads(stdout or "null")


@pytest.mark.parametrize("reason", ["x" * 201, "x\ny", "x\r", MARKER, ""])
def test_dismiss_refuses_invalid_reason(reason: str) -> None:
    api = FakeApi()
    with pytest.raises(review.ReviewError, match=r"^reason must be one line"):
        review.dismiss(api, REPO, DOC.pr, 942, reason)
    assert api.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"body": ""},
        {"state": "APPROVED"},
        {"body": MARKER.replace("pr=384", "pr=383")},
    ],
)
def test_dismiss_refuses_wrong_review(changes: dict[str, str]) -> None:
    api = FakeApi(ROW | changes)
    with pytest.raises(review.ReviewError, match="expected a COMMENT review"):
        review.dismiss(api, REPO, DOC.pr, 942, "stale")
    assert api.calls == [("GET", f"{REVIEWS}/942", None)]


def test_dismiss_cli_and_idempotence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reason = "r" * 200
    body = f"SUPERSEDED: {reason}\n{MARKER}"
    api = FakeApi(ROW, {}, ROW | {"body": body}, ROW | {"state": "DISMISSED"}, {})
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    monkeypatch.setattr(cli.pr, "origin_repository", lambda: REPO)
    args = ["dismiss", "384", "942", "--reason", reason]
    for _ in range(3):
        assert cli.main(args, "review") == 0
        assert capsys.readouterr() == ("superseded 942\n", "")
    get = ("GET", f"{REVIEWS}/942", None)
    assert api.calls == [get, ("PUT", f"{REVIEWS}/942", {"body": body}), get, get]
    assert cli.main(args, "review") == 1
    assert capsys.readouterr().err.startswith("review 942: expected a COMMENT review")


def test_reply_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi({"commit_id": HEAD}, {"id": 12})
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    monkeypatch.setattr(cli.pr, "origin_repository", lambda: REPO)
    assert cli.main(["reply", "384", "1", "--text", "Retested."], "review") == 0
    assert capsys.readouterr() == ("replied 12\n", "")
    body = "Re review 1 (head 01234567): Retested.\n<!-- tbhb-reply v1 review=1 -->"
    assert api.calls == [
        ("GET", f"{REVIEWS}/1", None),
        ("POST", f"repos/{REPO}/issues/384/comments", {"body": body}),
    ]


@pytest.mark.parametrize(
    ("text", "responses", "problem"),
    [
        ("<!-- tbhb-reply v1 review=12 -->", (), "reply text must not contain markers"),
        ("Retested.", ({"commit_id": "invalid"},), "review lacks a 40-hex commit ID"),
        ("Retested.", ({"commit_id": HEAD}, {}), "missing reply comment ID"),
    ],
)
def test_reply_refusals(text: str, responses: tuple[object, ...], problem: str) -> None:
    api = FakeApi(*responses)
    with pytest.raises(review.ReviewError, match=f"^{problem}$"):
        review.reply(api, REPO, DOC.pr, 942, text)
    assert len(api.calls) == len(responses)
