"""Post a review run's draft as a GitHub review, host-side."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha.agent import credentials, review_post, runfiles
from tbhbdev.alpha.agent.model import Harness, Profile, RunKind

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent import run_host
    from tbhbdev.alpha.agent.model import RunRequest, Settings

HEAD = "a" * 40
MINTED = "minted"


def draft(event: str = "COMMENT", head: str = HEAD, *, findings: bool = False) -> str:
    verdict = "NO-GO" if event == "REQUEST_CHANGES" else "GO"
    extra = "## Findings\n\n- `a.py:1` fix it.\n\n" if findings else ""
    return (
        f"---\npr: 7\ncommit_id: '{head}'\nevent: {event}\n---\n\n"
        f"## Verdict\n\n{verdict}\n\n{extra}## Verification\n\nRead the diff.\n"
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
    request = SimpleNamespace(
        repo="github.com/o/r",
        commit=HEAD,
        harness=Harness.CODEX,
        model="m1",
        effort="high",
        profile=Profile.REVIEW,
        kind=RunKind.EXEC,
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
STAMP = f"Run r1, judged head {HEAD}."


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
    assert payload["body"].startswith("## Verdict")
    body, trailer = payload["body"].rstrip("\n").rsplit("\n\n", 1)
    assert body.endswith(f"\n\n{STAMP}")
    key, value = trailer.split(": ", 1)
    assert key == agent_identity.KEY_GEN
    assert value == "LLM effort=high;harness=codex;model=m1"
    assert agent_identity.check_value(value) is None
    assert run.files.path("review").read_text() == "github.com/o/r#7 99"


@pytest.mark.parametrize(
    ("harness", "slug"), [(Harness.CLAUDE, "claude-code"), (Harness.AGY, "antigravity")]
)
def test_missing_session_is_omitted_even_when_the_agent_claims_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, slug: str
) -> None:
    text = draft() + "\nClaimed session `sha-256-64:0123456789abcdef`.\n"
    run, prepared = make(tmp_path, text)
    monkeypatch.setattr(run.request, "harness", harness)
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    assert review_post.posted(run, prepared)
    payload = api.calls[-1][2]
    assert payload is not None
    assert payload["body"].endswith(
        f"Generated-by: LLM effort=high;harness={slug};model=m1\n"
    )


def test_invalid_identity_refuses_to_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = make(tmp_path, draft())
    monkeypatch.setattr(run.request, "effort", "unknown")
    monkeypatch.setattr(credentials, "review_post_token", forbidden)
    monkeypatch.setattr(httpx, "request", forbidden)
    assert not review_post.posted(run, prepared)
    assert (
        "invalid LLM trailer: bad effort unknown" in run.files.path("log").read_text()
    )
    assert not run.files.path("review").exists()


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
    monkeypatch.setattr(run.request, "kind", kind)
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
            "REQUEST_CHANGES requires non-empty Findings",
        ),
        (draft("PENDING"), "review-7.md", "not one of"),
        (APPROVE_THEN_COMMENT, "review-7.md", "duplicate key"),
        ("---\npr: [\n---\n", "review-7.md", "invalid YAML"),
        (draft(head="b" * 40), "review-7.md", "isn't the commit"),
        ("---\npr: 7\n---\n", "review-7.md", "fails its checks"),
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


def test_the_host_can_refuse_a_draft_event_allowed_by_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared = make(tmp_path, draft("APPROVE"))
    monkeypatch.setattr(review_post, "ALLOWED_EVENTS", frozenset({"COMMENT"}))
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    monkeypatch.setattr(httpx, "request", forbidden)
    assert not review_post.posted(run, prepared)
    assert "can't post a APPROVE" in run.files.path("log").read_text()


def test_two_drafts_are_refused(tmp_path: Path) -> None:
    run, prepared = make(tmp_path, draft())
    directory = tmp_path / "ws" / ".git" / "tbhb-alpha"
    (directory / "review-8.md").write_text(draft())
    assert not review_post.posted(run, prepared)
    assert "found 2" in run.files.path("log").read_text()


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
