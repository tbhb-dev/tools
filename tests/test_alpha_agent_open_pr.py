"""Open a run's pull request from the draft in its pushed commit."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest

from tbhbdev.alpha.agent import open_pr

if TYPE_CHECKING:
    from pathlib import Path

DRAFT = """---
draft: false
base: main
---

# feat(example): add a thing

## Why

Because.

## Behavior change

A thing.

## Decisions

None.

## Needs human judgment

None.

## Risk

None.

## Verification

Ran it.
"""


def commit_draft(directory: Path, text: str | None) -> str:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=directory,
            text=True,
        ).strip()

    git("init", "-q")
    if text is not None:
        (directory / ".github").mkdir()
        (directory / ".github/PULL_REQUEST_DRAFT.md").write_text(text)
        git("add", ".")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "x",
    )
    return git("rev-parse", "HEAD")


def reply(status: int, data: object) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("POST", "https://x"))


def test_opens_the_pr_from_the_pushed_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = commit_draft(tmp_path, DRAFT)
    seen: dict[str, object] = {}

    def post(url: str, **options: object) -> httpx.Response:
        seen["url"] = url
        seen.update(options)
        return reply(201, {"number": 12})

    monkeypatch.setattr(httpx, "post", post)
    number = open_pr.open_pull_request(
        tmp_path, commit, "github.com/tbhb-dev/tools", "agent/r1", "tok"
    )
    assert number == 12
    assert seen["url"] == "https://api.github.com/repos/tbhb-dev/tools/pulls"
    payload = seen["json"]
    assert isinstance(payload, dict)
    assert payload["title"] == "feat(example): add a thing"
    assert payload["head"] == "agent/r1"
    assert payload["base"] == "main"
    assert payload["draft"] is False
    assert "## Why" in payload["body"]
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer tok"


def test_a_missing_draft_is_refused(tmp_path: Path) -> None:
    commit = commit_draft(tmp_path, None)
    with pytest.raises(
        open_pr.OpenRefusedError, match=r"no \.github/PULL_REQUEST_DRAFT"
    ):
        open_pr.open_pull_request(tmp_path, commit, "github.com/o/r", "agent/r", "t")


def test_a_draft_that_fails_its_check_is_refused(tmp_path: Path) -> None:
    commit = commit_draft(tmp_path, "# no front matter\n")
    with pytest.raises(open_pr.OpenRefusedError, match="fails its check at line"):
        open_pr.open_pull_request(tmp_path, commit, "github.com/o/r", "agent/r", "t")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (reply(422, {"message": "bad"}), "status 422"),
        (reply(201, {"id": 1}), "status 201"),
    ],
)
def test_a_refusing_api_is_reported_without_its_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    message: str,
) -> None:
    commit = commit_draft(tmp_path, DRAFT)

    def post(*_args: object, **_options: object) -> httpx.Response:
        return response

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(open_pr.OpenRefusedError, match=message):
        open_pr.open_pull_request(tmp_path, commit, "github.com/o/r", "agent/r", "t")


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), ValueError("x")])
def test_a_transport_failure_is_reported_by_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    commit = commit_draft(tmp_path, DRAFT)

    def post(*_args: object, **_options: object) -> httpx.Response:
        raise failure

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(open_pr.OpenRefusedError, match=type(failure).__name__):
        open_pr.open_pull_request(tmp_path, commit, "github.com/o/r", "agent/r", "t")
