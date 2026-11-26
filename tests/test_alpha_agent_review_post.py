"""Post a review run's draft as a GitHub review, host-side."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from tbhbdev import content
from tbhbdev.alpha import identity, review, review_doc
from tbhbdev.alpha.agent import credentials, review_post, runfiles
from tbhbdev.alpha.agent.decisions import request as requests
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile, RunKind, RunRequest

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent import run_host
    from tbhbdev.alpha.agent.model import Settings

pytestmark = pytest.mark.usefixtures("review_cue")  # noqa: V107

HEAD = "a" * 40
MINTED = "minted"


def draft(event: str = "COMMENT", head: str = HEAD, *, findings: bool = False) -> str:
    extra = (
        "## Findings\n\n- F1: P1 correctness Fix the data loss.\n\n" if findings else ""
    )
    return (
        f"---\npr: 7\ncommit_id: '{head}'\nevent: {event}\n---\n\n"
        "## Summary\n\nThe tests cover the changed path.\n\n"
        f"{extra}## Verification\n\n- Read the diff.\n"
    )


def make(
    tmp_path: Path, text: str | None, name: str = "review-7.md"
) -> tuple[run_host.Run, run_host.Prepared]:
    workspace = tmp_path / "ws"
    directory = workspace / ".git" / "tbhb-alpha"
    directory.mkdir(parents=True)
    if text is not None:
        (directory / name).write_text(text)
    files = runfiles.RunFiles(tmp_path / "runs", "r1")
    files.write("out.final", f"Review complete.\nVERDICT: GO {HEAD}\n")
    request = RunRequest(
        repo="github.com/o/r",
        commit=HEAD,
        harness=Harness.CODEX,
        effort="high",
        variant=None,
        model="gpt-6.2-sol",
        profile=Profile.REVIEW,
        kind=RunKind.EXEC,
        claude_profile=None,
        reason=None,
        account="a",
        timeout=None,
        class_=None,
        role=None,
        unsandboxed=False,
        authorized_by=None,
        startup_deadline_seconds=60,
    )
    run = SimpleNamespace(files=files, request=request, settings=object())
    prepared = SimpleNamespace(paths=SimpleNamespace(worktree=str(workspace)))
    return (
        cast("run_host.Run", run),
        cast("run_host.Prepared", prepared),
    )


def reply(status: int, data: object) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://x"))


def mint_token(_request: object, _settings: object) -> str | None:
    return MINTED


def no_token(_request: object, _settings: object) -> str | None:
    return None


def forbidden(*_args: object, **_options: object) -> httpx.Response:
    raise AssertionError


BASE = "https://api.github.com/repos/o/r/pulls/7"
STAMP = "Generated-by: LLM harness=codex;model=m1"


class Api:
    """A fake GitHub: records calls and answers each by method and path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []
        self.pull: httpx.Response | Exception = reply(200, {"head": {"sha": HEAD}})
        self.later = reply(200, {"head": {"sha": HEAD}})
        self.created: httpx.Response | Exception = reply(201, {"id": 99})
        monkeypatch.setattr(httpx, "request", self.request)

    def request(self, method: str, url: str, **options: object) -> httpx.Response:
        payload = cast("dict[str, str] | None", options.get("json"))
        self.calls.append((method, url, payload))
        gets = [c for c in self.calls if c[0] == "GET"]
        outcome = self.created if method == "POST" else self.pull
        if method == "PUT" or len(gets) > 1:
            outcome = self.later
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize(
    ("event", "head", "checks"),
    [
        ("APPROVE", HEAD, [("GET", BASE)]),
        ("REQUEST_CHANGES", "b" * 40, []),
        ("COMMENT", "b" * 40, []),
    ],
)
def test_posts_at_the_pinned_commit_with_identity_in_the_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    head: str,
    checks: list[tuple[str, str]],
) -> None:
    text = draft(event, HEAD.upper(), findings=event == "REQUEST_CHANGES")
    run, prepared = make(tmp_path, text)
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.pull = reply(200, {"head": {"sha": head}})
    assert review_post.posted(run, prepared)
    assert [(m, u) for m, u, _ in api.calls] == [
        *checks,
        ("POST", f"{BASE}/reviews"),
    ]
    payload = api.calls[-1][2]
    assert payload is not None
    assert payload["event"] == event
    assert payload["commit_id"] == HEAD
    assert payload["body"].startswith("## Summary")
    assert "VERDICT:" not in payload["body"]
    assert HEAD not in payload["body"]
    assert payload["body"].endswith(f"\n\n{review_post.stamp(run)}\n")
    assert (
        "Generated-by: LLM effort=high;harness=codex;launch=sha-256-64:"
        in payload["body"]
    )
    assert ";model=openai:gpt-6.2-sol;surface=exec" in payload["body"]
    assert run.files.path("review").read_text() == "github.com/o/r#7 99"


def test_a_run_name_posts_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    assert review_post.posted(run, prepared)

    run.files = runfiles.RunFiles(run.files.root, run.files.name)
    monkeypatch.setattr(credentials, "review_post_token", forbidden)
    assert review_post.posted(run, prepared)
    assert [method for method, _, _ in api.calls] == ["POST"]
    assert run.files.path("review").read_text() == "github.com/o/r#7 99"


OTHER_HEAD = "b" * 40


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "Review complete.",
        f"VERDICT: GO {HEAD}\nExtra text.",
        f"VERDICT: GO {OTHER_HEAD}",
        f"VERDICT: BLOCK {OTHER_HEAD}",
        f"VERDICT: NO-GO {OTHER_HEAD}",
        f"VERDICT: HOLD {HEAD}",
        f"VERDICT: GO {HEAD} extra",
        "VERDICT:GO",
        "verdict: GO",
    ],
)
def test_a_review_without_a_final_verdict_does_not_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> None:
    run, prepared = make(tmp_path, draft())
    run.files.path("out.final").unlink()
    if answer is not None:
        run.files.write("out", answer)
    monkeypatch.setattr(credentials, "review_post_token", forbidden)
    assert not review_post.posted(run, prepared)
    assert "review not posted: no verdict line" in run.files.path("log").read_text()


@pytest.mark.parametrize(
    "line",
    [
        "VERDICT: GO",
        "VERDICT: BLOCK",
        "VERDICT: NO-GO",
        f"VERDICT: GO {HEAD}",
        f"VERDICT: BLOCK {HEAD}",
        f"VERDICT: NO-GO {HEAD}",
    ],
)
def test_a_review_accepts_each_verdict_line_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    run, prepared = make(tmp_path, draft())
    run.files.write("out.final", f"Review complete.\n{line}\n")
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    Api(monkeypatch)
    assert review_post.posted(run, prepared)


@pytest.mark.parametrize(
    ("kind", "schema"),
    [(RunKind.EXEC, False), (RunKind.EXEC, True), (RunKind.START, False)],
)
def test_review_post_without_a_codex_final_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: RunKind, *, schema: bool
) -> None:
    run, prepared = make(tmp_path, draft("REQUEST_CHANGES", findings=True))
    run.files.path("out.final").unlink()
    if schema:
        run.files.write("schema.json", '{"type": "object"}')
        run.files.write("out", '{"result": "Review complete."}')
    elif kind is RunKind.EXEC:
        run.files.write("out", f"Review complete.\nVERDICT: NO-GO {HEAD}\n")
    monkeypatch.setattr(run, "request", replace(run.request, kind=kind))
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    assert review_post.posted(run, prepared)
    assert [method for method, _, _ in api.calls] == ["POST"]


APPROVE_THEN_COMMENT = draft("APPROVE").replace(
    "event: APPROVE", "event: APPROVE\nevent: COMMENT"
)


@pytest.mark.parametrize(
    ("text", "name", "message"),
    [
        (None, "review-7.md", "found 0"),
        (draft(), "notes.md", "found 0"),
        (
            draft("REQUEST_CHANGES"),
            "review-7.md",
            "REQUEST_CHANGES requires a P0 or P1 finding",
        ),
        (draft("PENDING"), "review-7.md", "not one of"),
        (APPROVE_THEN_COMMENT, "review-7.md", "duplicate key"),
        ("---\npr: [\n---\n", "review-7.md", "invalid YAML"),
        (draft(head="b" * 40), "review-7.md", "isn't the commit"),
        ("---\npr: 7\n---\n", "review-7.md", "fails its checks"),
        (
            draft("APPROVE").replace("## Summary", "## Verdict"),
            "review-7.md",
            "Rename Verdict to Summary and give the reasons for the review",
        ),
        (
            draft("APPROVE") + f"\nVERDICT: GO {HEAD}\n",
            "review-7.md",
            "Remove the standalone verdict line from Verification",
        ),
        (
            draft() + f"\n<!-- tbhb-review v2 {'e' * 64} -->\n",
            "review-7.md",
            "Write Verification as one to six unnumbered bullets",
        ),
    ],
)
def test_a_bad_draft_posts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str | None,
    name: str,
    message: str,
) -> None:
    run, prepared = make(tmp_path, text, name)
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    monkeypatch.setattr(httpx, "request", forbidden)
    assert not review_post.posted(run, prepared)
    assert message in run.files.path("log").read_text()


def test_host_refuses_a_standalone_verdict_line() -> None:
    text = draft("APPROVE").replace("The tests cover the changed path.", "VERDICT: GO")
    sub = review_post.Submission("github.com/o/r", 7, text, HEAD, STAMP)
    with pytest.raises(
        review_post.PostRefusedError,
        match="Remove the standalone verdict line from Summary",
    ):
        review_post.validate(sub)


@pytest.mark.parametrize(
    "summary",
    [
        "Go is the implementation language.",
        "The tests pass. Go is the implementation language.",
        "Block sizes stay within existing limits.",
        "Approved requests retain their original ordering.",
        "The handler returns the `APPROVE` enum.",
    ],
)
def test_host_accepts_ordinary_prose_in_summary(summary: str) -> None:
    text = draft("APPROVE").replace("The tests cover the changed path.", summary)
    sub = review_post.Submission("github.com/o/r", 7, text, HEAD, STAMP)
    fields, body = review_post.validate(sub)
    assert fields["event"] == "APPROVE"
    assert f"## Summary\n\n{summary}\n" in body


def test_two_drafts_are_refused(tmp_path: Path) -> None:
    run, prepared = make(tmp_path, draft())
    directory = tmp_path / "ws" / ".git" / "tbhb-alpha"
    (directory / "review-8.md").write_text(draft())
    assert not review_post.posted(run, prepared)
    assert "found 2" in run.files.path("log").read_text()


def test_posts_from_a_fresh_worktree_without_synced_styles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = make(tmp_path, draft("APPROVE"))
    workspace = tmp_path / "ws"
    (workspace / ".vale.ini").write_text("BasedOnStyles = Google\n")
    (workspace / ".vale" / "project").mkdir(parents=True)
    monkeypatch.setattr(review_doc, "lint_post", forbidden)
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    assert review_post.posted(run, prepared)
    assert [method for method, _, _ in api.calls] == ["GET", "POST"]


def document() -> str:
    _, _, body = draft("APPROVE").rpartition("---\n")
    return (
        f"---\nschema: 2\nrepo: o/r\npr: 7\nhead_sha: '{HEAD}'\n"
        "tier: 2\nverdict: GO\nrun:\n  name: r1\n  provider: openai\n"
        "  model: gpt-6.2-sol\n  variant: a\n  host: reviewer.example.org\n"
        f"---\n{body}"
    )


@pytest.mark.parametrize("verdict", ["GO", "NO-GO"])
def test_host_and_cli_prefer_the_same_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    text = document()
    if verdict == "NO-GO":
        text = text.replace("verdict: GO", "verdict: NO-GO").replace(
            "## Verification",
            "## Findings\n\n- F1: P1 correctness Fix the cache.\n\n## Verification",
        )
    run, prepared = make(tmp_path, text, "review-7.doc.md")
    directory = tmp_path / "ws" / ".git" / "tbhb-alpha"
    (directory / "review-7.md").write_text("An invalid standard draft.")
    monkeypatch.setattr(review, "draft_directory", lambda: directory)
    assert review.main(["check", "7", "--no-lint"], "review") == 0
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    assert review_post.posted(run, prepared)
    payload = api.calls[-1][2]
    assert payload is not None
    assert payload["event"] == ("APPROVE" if verdict == "GO" else "REQUEST_CHANGES")
    assert payload["commit_id"] == HEAD
    assert not content.check_review(
        payload["body"], metadata=review_doc.parse(text).metadata, posted=True
    )
    assert review_doc.parse_marker(payload["body"]) == review_doc.marker_id(
        "o/r", 7, "r1"
    )


@pytest.mark.parametrize(
    ("original", "replacement", "message"),
    [
        ("pr: 7", "pr: 8", "must name pull request 7"),
        ("repo: o/r", "repo: o/s", "isn't the run's repository"),
        (HEAD, "b" * 40, "isn't the commit the run reviewed"),
        ("- Read the diff.", "Read the diff.", "Write Verification"),
    ],
)
def test_host_refuses_invalid_schema_two_drafts(
    original: str, replacement: str, message: str
) -> None:
    text = document().replace(original, replacement)
    sub = review_post.Submission("github.com/o/r", 7, text, HEAD, STAMP, document=True)
    with pytest.raises(review_post.PostRefusedError, match=message):
        review_post.validate(sub)


@pytest.mark.parametrize(
    "pull",
    [reply(200, {"head": {"sha": "b" * 40}}), reply(200, {}), reply(200, [])],
)
def test_an_approval_requires_one_read_of_the_pinned_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pull: httpx.Response
) -> None:
    run, prepared = make(tmp_path, draft("APPROVE"))
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.pull = pull
    assert not review_post.posted(run, prepared)
    assert "head moved" in run.files.path("log").read_text()
    assert api.calls == [("GET", BASE, None)]


def test_a_missing_token_blocks_the_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", no_token)
    monkeypatch.setattr(httpx, "request", forbidden)
    assert not review_post.posted(run, prepared)
    assert "token unavailable" in run.files.path("log").read_text()


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (reply(403, {"message": "no"}), "status 403"),
        (httpx.ConnectError("x"), "ConnectError"),
    ],
)
def test_post_failures_are_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: httpx.Response | Exception,
    message: str,
) -> None:
    run, prepared = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.created = outcome
    assert not review_post.posted(run, prepared)
    assert message in run.files.path("log").read_text()
    assert not run.files.path("review").exists()


def test_any_2xx_post_is_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    Api(monkeypatch).created = reply(202, {"id": 8})
    assert review_post.posted(run, prepared)
    assert run.files.path("review").read_text() == "github.com/o/r#7 8"


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(204),
        reply(201, {}),
        reply(202, {}),
        reply(200, []),
        reply(201, {"id": "x"}),
        httpx.Response(200, text="OK"),
        httpx.Response(200, text="not json"),
    ],
)
def test_a_2xx_post_without_an_integer_id_counts_as_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: httpx.Response
) -> None:
    run, prepared = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    Api(monkeypatch).created = outcome
    assert review_post.posted(run, prepared)
    assert run.files.path("review").read_text() == "github.com/o/r#7 unknown"


def request_for(profile: Profile) -> RunRequest:
    request = SimpleNamespace(profile=profile, account="a", repo="github.com/o/r")
    return cast("RunRequest", request)


def settings_for_account() -> Settings:
    return cast("Settings", SimpleNamespace(credentials=[SimpleNamespace(account="a")]))


def test_the_post_token_is_scoped_to_pull_request_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, str]] = []

    def mint(_references: object, repository: str, permissions: dict[str, str]) -> str:
        assert repository == "github.com/o/r"
        seen.append(permissions)
        return MINTED

    monkeypatch.setattr(credentials, "installation_token", mint)
    settings = settings_for_account()
    assert credentials.review_post_token(request_for(Profile.BUILD), settings) is None
    token = credentials.review_post_token(request_for(Profile.REVIEW), settings)
    assert token == MINTED
    assert seen == [{"pull_requests": "write"}]


def test_a_failed_token_request_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> str:
        message = "no"
        raise credentials.Invalid(message)

    monkeypatch.setattr(credentials, "installation_token", fail)
    request = request_for(Profile.REVIEW)
    assert credentials.review_post_token(request, settings_for_account()) is None


def test_review_attribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = make(tmp_path, draft())
    request = replace(run.request, harness=Harness.AGY, variant="a")
    monkeypatch.setattr(run, "request", request)
    trailer = review_post.stamp(run)
    assert "harness=antigravity;" in trailer
    assert trailer.endswith(";variant=a")
    assert identity.check_message(trailer) == []
    monkeypatch.setattr(run, "request", replace(request, variant="a;b"))
    with pytest.raises(review_post.PostRefusedError, match="invalid LLM trailer"):
        review_post.stamp(run)
    monkeypatch.setattr(run, "request", replace(request, model="a;b"))
    with pytest.raises(review_post.PostRefusedError, match="invalid LLM trailer"):
        review_post.stamp(run)


@pytest.mark.parametrize(
    ("harness", "model", "effort", "expected"),
    [
        (Harness.CODEX, "gpt-6.2-sol", "xhigh", "effort=xhigh;harness=codex;"),
        (Harness.CLAUDE, "claude-opus-5-5", "high", "effort=high;harness=claude-code;"),
        (
            Harness.AGY,
            "gemini-3.8-flash-low",
            "auto",
            "effort=low;harness=antigravity;",
        ),
        (
            Harness.AGY,
            "gemini-3.8-flash-low",
            "high",
            "effort=low;harness=antigravity;",
        ),
        (Harness.AGY, "gemini-unknown", "auto", "LLM harness=antigravity;"),
        (Harness.CODEX, "gpt-6.2-sol", "auto", None),
        (Harness.CLAUDE, "claude-opus-5-5", "auto", None),
        (Harness.AGY, "gemini-3.8-flash-low", "typo", None),
    ],
)
def test_review_launch_and_post_share_the_effort_rule(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    model: str,
    effort: str,
    expected: str | None,
) -> None:
    run, _ = make(tmp_path, draft())
    request = replace(
        run.request,
        harness=harness,
        model=model,
        effort=effort,
        reason="review",
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        timeout=60,
    )
    monkeypatch.setattr(run, "request", request)
    if expected is None:
        message = f"request effort {effort!r} is unsupported for {harness.value}"
        with pytest.raises(Invalid, match=message):
            requests.validate(run.request, host_allows_unsandboxed=False)
        with pytest.raises(review_post.PostRefusedError, match=message):
            review_post.stamp(run)
    else:
        requests.validate(run.request, host_allows_unsandboxed=False)
        assert expected in review_post.stamp(run)
