"""Post a review run's draft as a GitHub review, host-side."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from tbhbdev.alpha.agent import credentials, review_post
from tbhbdev.alpha.agent.model import Profile

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent import run_host
    from tbhbdev.alpha.agent.model import RunRequest, Settings

HEAD = "a" * 40
MINTED = "minted"


def draft(event: str = "COMMENT", head: str = HEAD, *, findings: bool = False) -> str:
    extra = "## Findings\n\n- `a.py:1` fix it.\n\n" if findings else ""
    return (
        f"---\npr: 7\ncommit_id: '{head}'\nevent: {event}\n---\n\n"
        f"## Verdict\n\nLooks fine.\n\n{extra}## Verification\n\nRead the diff.\n"
    )


def make(
    tmp_path: Path, text: str | None, name: str = "review-7.md"
) -> tuple[run_host.Run, run_host.Prepared, dict[str, str]]:
    workspace = tmp_path / "ws"
    directory = workspace / ".git" / "tbhb-alpha"
    directory.mkdir(parents=True)
    if text is not None:
        (directory / name).write_text(text)
    written: dict[str, str] = {}
    files = SimpleNamespace(name="r1", write=written.__setitem__)
    request = SimpleNamespace(
        repo="github.com/o/r",
        commit=HEAD,
        harness=SimpleNamespace(value="codex"),
        model="m1",
        profile=Profile.REVIEW,
    )
    run = SimpleNamespace(files=files, request=request, settings=object())
    prepared = SimpleNamespace(paths=SimpleNamespace(worktree=str(workspace)))
    return (
        cast("run_host.Run", run),
        cast("run_host.Prepared", prepared),
        written,
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
STAMP = f"Run r1, codex m1, judged head {HEAD}."


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


def test_posts_the_comment_with_identity_in_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    assert review_post.posted(run, prepared)
    assert [(m, u) for m, u, _ in api.calls] == [
        ("GET", BASE),
        ("POST", f"{BASE}/reviews"),
    ]
    payload = api.calls[-1][2]
    assert payload is not None
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == HEAD
    assert payload["body"].startswith("## Verdict")
    assert payload["body"].endswith(f"\n---\n{STAMP}\n")
    assert written == {"review": "github.com/o/r#7 99"}


APPROVE_THEN_COMMENT = draft("APPROVE").replace(
    "event: APPROVE", "event: APPROVE\nevent: COMMENT"
)


@pytest.mark.parametrize(
    ("text", "name", "message"),
    [
        (None, "review-7.md", "found 0"),
        (draft(), "notes.md", "found 0"),
        (draft("APPROVE"), "review-7.md", "can't post a APPROVE"),
        (
            draft("REQUEST_CHANGES", findings=True),
            "review-7.md",
            "can't post a REQUEST",
        ),
        (APPROVE_THEN_COMMENT, "review-7.md", "duplicate key"),
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
    run, prepared, written = make(tmp_path, text, name)
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    monkeypatch.setattr(httpx, "request", forbidden)
    assert not review_post.posted(run, prepared)
    assert message in written["log"]


def test_two_drafts_are_refused(tmp_path: Path) -> None:
    run, prepared, written = make(tmp_path, draft())
    directory = tmp_path / "ws" / ".git" / "tbhb-alpha"
    (directory / "review-8.md").write_text(draft())
    assert not review_post.posted(run, prepared)
    assert "found 2" in written["log"]


@pytest.mark.parametrize(
    "pull",
    [reply(200, {"head": {"sha": "b" * 40}}), reply(200, {}), reply(200, [])],
)
def test_a_moved_head_blocks_the_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pull: httpx.Response
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.pull = pull
    assert not review_post.posted(run, prepared)
    assert "head moved" in written["log"]
    assert all(m == "GET" for m, _, _ in api.calls)


def test_a_missing_token_blocks_the_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", no_token)
    monkeypatch.setattr(httpx, "request", forbidden)
    assert not review_post.posted(run, prepared)
    assert "token unavailable" in written["log"]


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
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.created = outcome
    assert not review_post.posted(run, prepared)
    assert message in written["log"]
    assert "review" not in written


def test_any_2xx_post_is_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    Api(monkeypatch).created = reply(202, {"id": 8})
    assert review_post.posted(run, prepared)
    assert written == {"review": "github.com/o/r#7 8"}


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
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    Api(monkeypatch).created = outcome
    assert review_post.posted(run, prepared)
    assert written == {"review": "github.com/o/r#7 unknown"}


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
