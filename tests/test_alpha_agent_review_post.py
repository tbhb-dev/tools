"""Post a review run's draft as a GitHub review, host-side."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from tbhbdev.alpha import review
from tbhbdev.alpha.agent import credentials, review_post
from tbhbdev.alpha.agent.model import Profile

if TYPE_CHECKING:
    from collections.abc import Callable
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
        self.reviews: list[httpx.Response | Exception] = [reply(200, [])]
        self.created: httpx.Response | Exception = reply(201, {"id": 99})
        monkeypatch.setattr(httpx, "request", self.request)

    def request(self, method: str, url: str, **options: object) -> httpx.Response:
        payload = cast("dict[str, str] | None", options.get("json"))
        self.calls.append((method, url, payload))
        if method == "POST":
            outcome = self.created
        elif url.startswith(f"{BASE}/reviews"):
            outcome = self.reviews.pop(0)
        else:
            outcome = self.pull
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def draft_at(git_dir: Path) -> Callable[[int], Path]:
    def path(number: int) -> Path:
        return git_dir / "tbhb-alpha" / f"review-{number}.md"

    return path


def test_posts_the_comment_with_identity_in_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    assert review_post.posted(run, prepared)
    assert [(m, u) for m, u, _ in api.calls] == [
        ("GET", BASE),
        ("GET", f"{BASE}/reviews?per_page=100&page=1"),
        ("POST", f"{BASE}/reviews"),
    ]
    payload = api.calls[-1][2]
    assert payload is not None
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == HEAD
    assert payload["body"].startswith("## Verdict")
    assert payload["body"].endswith(f"\n---\n{STAMP}\n")
    assert written == {"review": "github.com/o/r#7 99"}


def test_request_changes_posts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, prepared, _ = make(tmp_path, draft("REQUEST_CHANGES", findings=True))
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    Api(monkeypatch)
    assert review_post.posted(run, prepared)


APPROVE_THEN_COMMENT = draft("APPROVE").replace(
    "event: APPROVE", "event: APPROVE\nevent: COMMENT"
)


@pytest.mark.parametrize(
    ("text", "name", "message"),
    [
        (None, "review-7.md", "found 0"),
        (draft(), "notes.md", "found 0"),
        (draft("APPROVE"), "review-7.md", "can't post a APPROVE"),
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


def test_a_repeat_returns_the_existing_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.reviews = [
        reply(200, [{"id": "x", "body": STAMP}, "junk", {"id": 5, "body": "other"}]),
        reply(200, [{"id": 41, "body": f"text\n---\n{STAMP}\n"}]),
    ]
    api.reviews[0] = reply(200, [{"id": 1, "body": "z"}] * 99 + [{"id": 2}])
    api.reviews.insert(0, api.reviews[0])
    api.reviews.pop(0)
    assert review_post.posted(run, prepared)
    assert written == {"review": "github.com/o/r#7 41"}
    assert not any(m == "POST" for m, _, _ in api.calls)


def test_a_stamp_on_the_first_page_returns_that_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.reviews = [
        reply(200, ["junk", {"id": "x", "body": STAMP}, {"id": 5, "body": STAMP}])
    ]
    assert review_post.posted(run, prepared)
    assert written == {"review": "github.com/o/r#7 5"}


def test_too_many_review_pages_block_the_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    full = reply(200, [{"id": 1, "body": "z"}] * review_post.PAGE)
    api.reviews = [full] * review_post.MAX_PAGES
    assert not review_post.posted(run, prepared)
    assert "too many reviews" in written["log"]


def test_a_review_list_that_is_not_a_list_blocks_the_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, prepared, written = make(tmp_path, draft())
    monkeypatch.setattr(credentials, "review_post_token", mint_token)
    api = Api(monkeypatch)
    api.reviews = [reply(200, {})]
    assert not review_post.posted(run, prepared)
    assert "no review list" in written["log"]


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
        (reply(201, {"id": "x"}), "no review id"),
        (reply(201, []), "no review id"),
        (httpx.Response(200, text="not json"), "request failed"),
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


def test_create_posts_this_worktrees_draft_with_the_stdin_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make(tmp_path, draft())
    git_dir = tmp_path / "ws" / ".git"
    monkeypatch.setattr(review, "draft_path", draft_at(git_dir))
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    Api(monkeypatch)
    args = ["7", "--repo", "github.com/o/r", "--commit", HEAD, "--stamp", STAMP]
    assert review.main(["create", *args], "review") == 0
    assert capsys.readouterr().out == "99\n"


def test_create_reports_a_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make(tmp_path, draft("APPROVE"))
    git_dir = tmp_path / "ws" / ".git"
    monkeypatch.setattr(review, "draft_path", draft_at(git_dir))
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    monkeypatch.setattr(httpx, "request", forbidden)
    args = ["7", "--repo", "github.com/o/r", "--commit", HEAD, "--stamp", STAMP]
    assert review.main(["create", *args], "review") == 1
    assert "can't post" in capsys.readouterr().err


def test_create_reports_a_missing_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(review, "draft_path", draft_at(tmp_path / "x" / "y"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    args = ["7", "--repo", "github.com/o/r", "--commit", HEAD, "--stamp", STAMP]
    assert review.main(["create", *args], "review") == 1
    assert "No such file" in capsys.readouterr().err


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
