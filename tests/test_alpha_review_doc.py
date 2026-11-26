from __future__ import annotations

import json
from dataclasses import replace
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev.alpha import review as cli
from tbhbdev.alpha import review_doc as review
from tbhbdev.alpha.review_doc import Existing, Marker, ReviewDoc

if TYPE_CHECKING:
    from pathlib import Path

HEAD = "0123456789abcdef0123456789abcdef01234567"
REPO = "tbhb-dev/tools"
BODY = """## Summary

A warm cache can return a stale value.

## Findings

- F1: P1 correctness Return the saved value.

## Verification

- Read the cache.
"""
PREAMBLE = f"""---
schema: 2
repo: {REPO}
pr: 384
head_sha: '{HEAD}'
tier: 2
verdict: NO-GO
run:
  name: review-build
  provider: openai
  model: gpt-6
  variant: a
  host: reviewer.example.org
---
"""
DOC = review.parse(PREAMBLE + BODY)
GO = review.parse(PREAMBLE.replace("NO-GO", "GO") + BODY.replace("P1", "P2"))
MARKER = f"<!-- tbhb-review v1 repo={REPO} pr=384 head={HEAD} run=review-build -->"
IDENTITY = "LLM harness=codex;model=openai:gpt-6;session=sha-256-64:0123456789abcdef"
TRAILER = f"Generated-by: {IDENTITY}"
KEY = Marker(REPO, 384, HEAD, "review-build")
EXISTING = Existing(942, "reviews[bot]", KEY, "COMMENTED", f"{BODY}\n{MARKER}\n")
PULL = f"repos/{REPO}/pulls/384"
REVIEWS = f"{PULL}/reviews"
PAGE = f"{REVIEWS}?per_page=100&page="
LIVE = {"head": {"sha": HEAD}}
USER = {"login": EXISTING.author}
ROW = {"id": 942, "user": USER, "state": "COMMENTED", "body": MARKER, "commit_id": HEAD}
DECISION_ROW = ROW | {"state": "CHANGES_REQUESTED"}


@pytest.fixture(autouse=True)
def stub_vale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review.pr, "lint", Mock(return_value=[]))


@pytest.mark.parametrize(
    "front", ["", "---\npr: [\n---\n", PREAMBLE.replace("pr: 384", "pr: true")]
)
def test_parse_refuses_malformed_drafts(front: str) -> None:
    with pytest.raises(review.ReviewError):
        review.parse(front + BODY)


def test_marker_uses_last_matching_line() -> None:
    assert HEAD not in review.render(DOC)
    assert review.parse_marker(review.render(DOC)) == review.marker_id(
        REPO, 384, DOC.run
    )
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
        DECISION_ROW | {"user": {"login": "other"}},
        DECISION_ROW | {"body": ""},
        DECISION_ROW | {"body": f"quoted {MARKER}"},
        DECISION_ROW | {"body": MARKER.replace("pr=384", "pr=385")},
        ROW | {"state": "DISMISSED"},
        DECISION_ROW | {"body": review.render(DOC), "commit_id": "b" * 40},
        ROW,
        ROW | {"state": "APPROVED"},
        DECISION_ROW | {"body": f"SUPERSEDED: reason\n{MARKER}"},
    ]
    first = [DECISION_ROW, *ignored, *([None] * 90)]
    api = FakeApi(
        first,
        [
            DECISION_ROW | {"id": 22},
            DECISION_ROW | {"id": 23, "body": f"{review.render(DOC)}\n{TRAILER}\n"},
            *ignored,
        ],
    )
    assert (
        review.find_existing(
            api, REPO, 384, HEAD, KEY.run, "Reviews[bot]", state="CHANGES_REQUESTED"
        )
        == 23
    )
    assert api.calls == [("GET", PAGE + "1", None), ("GET", PAGE + "2", None)]


def test_find_existing_refuses_non_list() -> None:
    with pytest.raises(review.ReviewError, match=r"^expected a list of reviews$"):
        review.find_existing(
            FakeApi({}), REPO, 384, HEAD, KEY.run, EXISTING.author, state="APPROVED"
        )


@pytest.mark.usefixtures("review_cue")
def test_post_refuses_moved_head() -> None:
    api = FakeApi({"head": {"sha": "f" * 40}})
    with pytest.raises(review.ReviewError, match=r"^pull request head has moved$"):
        review.post(api, DOC, author=EXISTING.author, identity_value=IDENTITY)
    assert api.calls == [("GET", PULL, None)]


@pytest.fixture
def draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    stem = tmp_path / "review-384"
    monkeypatch.setattr(cli, "draft_directory", lambda: tmp_path)
    monkeypatch.setattr(cli.pr, "origin_repository", lambda: REPO)
    monkeypatch.setattr(cli.pr, "posting_value", Mock(return_value=IDENTITY))
    return stem.with_suffix(".doc.md")


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("doc", "response", "expected"),
    [
        (DOC, {"id": 16}, 16),
        (GO, {}, None),
    ],
)
def test_post_and_cli_send_the_verdict_as_event(  # noqa: PLR0913, PLR0917
    doc: ReviewDoc,
    response: object,
    expected: int | None,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = f"{review.render(doc)}\n{TRAILER}\n"
    assert review.parse_marker(body) == review.marker_id(REPO, 384, doc.run)
    event = "APPROVE" if doc.verdict == "GO" else "REQUEST_CHANGES"
    calls = [
        ("GET", PULL, None),
        ("GET", PAGE + "1", None),
        ("POST", REVIEWS, {"commit_id": HEAD, "body": body, "event": event}),
    ]
    api = FakeApi(LIVE, [], response)
    assert review.post(api, doc, author=EXISTING.author, identity_value=IDENTITY) == (
        "posted",
        expected,
    )
    assert api.calls == calls
    api = FakeApi(USER, LIVE, [], response)
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    draft.with_name("review-384.md").write_text("Invalid standard draft.\n")
    draft.write_text(PREAMBLE.replace("NO-GO", doc.verdict) + doc.body)
    assert cli.main(["check", "384"], "review") == 0
    assert capsys.readouterr() == ("", "")
    assert api.calls == []
    assert cli.main(["post", "384"], "review") == 0
    assert api.calls == [("GET", "user", None), *calls]
    output = "posted\n" if expected is None else f"posted {expected}\n"
    assert capsys.readouterr() == (output, "")


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
def test_post_cli_uses_draft_event(
    event: str,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = GO.body if event == "APPROVE" else BODY
    standard = draft.with_name("review-384.md")
    standard.write_text(
        f"---\npr: 384\ncommit_id: {HEAD.upper()}\nevent: {event}\n---\n{body}"
    )
    api = FakeApi(LIVE, {"id": 16})
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    assert cli.main(["post", "384"], "review") == 0
    assert capsys.readouterr() == ("posted 16\n", "")
    posted_body = f"{body}\n{TRAILER}\n"
    assert api.calls == [
        ("GET", PULL, None),
        ("POST", REVIEWS, {"commit_id": HEAD, "body": posted_body, "event": event}),
    ]


@pytest.mark.usefixtures("review_cue")
@pytest.mark.parametrize(
    ("event", "pr_number", "problem"),
    [
        ("COMMENT", 384, "set event to APPROVE or REQUEST_CHANGES"),
        ("APPROVE", 383, "line 2: frontmatter $.pr: 384 was expected"),
        ("APPROVE", 384, "Vale refused"),
    ],
)
def test_post_cli_refuses_a_draft_without_a_valid_decision(  # noqa: PLR0913, PLR0917
    event: str,
    pr_number: int,
    problem: str,
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = GO.body
    standard = draft.with_name("review-384.md")
    standard.write_text(
        f"---\npr: {pr_number}\ncommit_id: {HEAD}\nevent: {event}\n---\n{body}"
    )
    monkeypatch.setattr(review, "lint_post", Mock(return_value=["Vale refused"]))
    api = FakeApi()
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    assert cli.main(["post", "384"], "review") == 1
    assert capsys.readouterr().err.startswith(f"{standard}: {problem}")
    assert api.calls == []


@pytest.mark.usefixtures("review_cue")
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


@pytest.mark.usefixtures("review_cue")
def test_post_cli_exists_and_refusals(
    draft: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeApi(LIVE, [DECISION_ROW], {})
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    args = ["post", "384", "--author", EXISTING.author]
    assert cli.main(args, "review") == 1
    standard = draft.with_name("review-384.md")
    assert capsys.readouterr().err.startswith(f"{standard}: ")
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
    assert capsys.readouterr() == (
        "",
        f"{draft}: Findings: P0 and P1 findings require REQUEST_CHANGES\n",
    )


@pytest.mark.usefixtures("review_cue")
def test_check_and_post_prefer_the_same_document(
    draft: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    standard = draft.with_name("review-384.md")
    standard.write_text(
        f"---\npr: 384\ncommit_id: {HEAD}\nevent: APPROVE\n---\n{GO.body}"
    )
    api = FakeApi()
    monkeypatch.setattr(cli, "GhApi", lambda: api)
    check = ["check", "384", "--no-lint"]
    assert cli.main(check, "review") == 0
    draft.write_text(PREAMBLE.replace("pr: 384", "pr: 383") + BODY)
    assert cli.main(check, "review") == 1
    assert capsys.readouterr() == (
        "",
        f"{draft}:1: draft must name pull request 384\n",
    )
    draft.write_text(PREAMBLE + BODY.replace("- Read the cache.", "Read the cache."))
    message = (
        "Write Verification as one to six unnumbered bullets of at most 20 words each"
    )
    assert cli.main(["check", "384"], "review") == 1
    first = PREAMBLE.count("\n") + 1
    assert capsys.readouterr() == ("", f"{draft}:{first}: {message}\n")
    assert cli.main(["post", "384", "--author", EXISTING.author], "review") == 1
    assert capsys.readouterr() == ("", f"{draft}: {message}\n")
    draft.write_text(PREAMBLE + BODY)
    assert cli.main(check, "review") == 0
    assert capsys.readouterr() == ("", "")
    assert api.calls == []


@pytest.mark.usefixtures("review_cue")
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
