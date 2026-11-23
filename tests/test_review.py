"""Review tier policy and commit status tests."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import cast

import httpx
import pytest
from test_alpha_agent_credentials import pull_requests_settings

from tbhbdev import cli, review
from tbhbdev.alpha.agent.decisions.repo import DEFAULT_GIT_HOST
from tbhbdev.alpha.agent.model import Credentials, Invalid

POLICY = Path(__file__).parent / "testdata/review/review-tiers.toml"
RULES = review.load_rules(POLICY.read_text(encoding="utf-8"), "global")
HEAD, BASE = "a" * 40, "b" * 40
ROOT = "/repos/tbhb-dev/wiki"
PULL = f"{ROOT}/pulls/7"
STATUS = f"{ROOT}/statuses/{HEAD}"
PROSE_RESULT = "tier 0: prose-only change"
LOCAL_RESULT = "tier 2: local path rule 1"
ROW: review.ChangedFile = {
    "filename": "notes.md",
    "status": "modified",
    "additions": 1,
    "deletions": 1,
    "patch": "@@ -1,2 +1,2 @@\n rmtree\n-old\n+new",
}


@pytest.mark.parametrize(
    "text",
    [
        "tier=0",
        "[tier.3]",
        "[tier.1]\npaths=1",
        "[tier.1]\npaths=[0]",
        "[tier.2]\npatterns=['(']",
    ],
)
def test_invalid_policy(text: str) -> None:
    with pytest.raises(Invalid, match="invalid global"):
        review.load_rules(text, "global")


@pytest.mark.parametrize(
    ("field", "value", "tier"),
    [
        ("filename", "notes.md", 0),
        ("filename", "designs/notes.md", 1),
        ("filename", review.LOCAL_POLICY, 2),
        ("previous_filename", ".github/workflows/check.yml", 2),
        ("status", "removed", 2),
        ("patch", "+truncated", 2),
        ("patch", "-rmtree\n+new", 2),
        ("patch", "-old\n+git push --force-with-lease", 2),
    ],
)
def test_classifies_changed_files(field: str, value: str, tier: int) -> None:
    row = cast("review.ChangedFile", ROW | {field: value})
    assert review.classify("Wiki", [row], RULES, [])[0] == tier
    assert review.classify("tools", [row], RULES, [])[0] == max(1, tier)


def test_empty_and_binary_diffs() -> None:
    assert review.classify("wiki", [], RULES, []) == (1, ["empty diff"])
    row = ROW.copy()
    del row["patch"]
    row.update(additions=0, deletions=0)
    assert review.classify("wiki", [row], RULES, [])[0] == 1


@pytest.mark.parametrize(
    "spec",
    ["0]\npaths=['absent']", "1]\npaths=['.codex/**']", "1]\npaths=['*.md']"],
)
def test_refuses_lower_tiers(spec: str) -> None:
    local = review.load_rules("[tier." + spec, "local")
    with pytest.raises(Invalid, match="would lower"):
        review.classify("wiki", [{**ROW, "status": "removed"}], RULES, local)


def metadata(head: str) -> httpx.Response:
    return httpx.Response(
        200, json={"head": {"sha": head}, "base": {"sha": BASE}, "changed_files": 2}
    )


def exchange(case: str) -> httpx.MockTransport:
    rows: list[review.ChangedFile] = [] if case == "rows" else [ROW]
    local = httpx.Response(
        404 if case == "404" else 200, text="[tier.2]\npaths=['*.md']"
    )
    second = httpx.Response(200, json=[{**ROW, "filename": "guide.md"}])
    steps = iter(
        [
            (PULL, None, metadata("" if case == "meta" else HEAD)),
            (PULL + "/files", "1", httpx.Response(200, json=rows)),
            (PULL + "/files", "2", second),
            (f"{ROOT}/contents/{review.LOCAL_POLICY}", BASE, local),
            (PULL, None, metadata(BASE if case == "move" else HEAD)),
            (STATUS, None, httpx.Response(403 if case == "403" else 201)),
        ]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        path, ref, response = next(steps)
        assert request.headers["Authorization"] == "Bearer fake"
        assert request.url.path == path
        assert request.url.params.get("page", request.url.params.get("ref")) == ref
        if path == STATUS:
            assert case != "move"
            assert request.method == "POST"
            reason = PROSE_RESULT if case == "404" else LOCAL_RESULT
            assert json.loads(request.content) == {
                "context": "review-tier",
                "state": "success",
                "description": reason,
            }
        return response

    return httpx.MockTransport(respond)


@pytest.mark.parametrize(
    "case",
    ["ok", "404", "move", "meta", "rows", "403", "cfg", "app", "mint", "io", "git"],
)
def test_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    def mint(_refs: Credentials, repository: str, permissions: dict[str, str]) -> str:
        assert repository == "tbhb-dev/wiki"
        assert permissions == {
            "contents": "read",
            "pull_requests": "read",
            "statuses": "write",
        }
        if case == "mint":
            raise Invalid
        return "fake"

    def git(*_args: str) -> tuple[int, str]:
        return int(case == "git"), f"git@{DEFAULT_GIT_HOST}:tbhb-dev/wiki.git"

    monkeypatch.setattr(review.repo, "git", git)
    monkeypatch.setattr(
        review.httpx, "Client", partial(httpx.Client, transport=exchange(case))
    )
    monkeypatch.setattr(review.settings, "load", pull_requests_settings)
    monkeypatch.setattr(review.credentials, "pull_requests_token", mint)
    monkeypatch.setattr(review, "PAGE_SIZE", 1)
    monkeypatch.delenv(review.POLICY_ENV, raising=False)
    args = ["review", "tier", "7"]
    if case == "ok":
        args.extend(("--config", str(POLICY)))
    elif case != "cfg":
        monkeypatch.setenv(
            review.POLICY_ENV, str(POLICY.parent if case == "io" else POLICY)
        )
    if case == "app":
        args.extend(("--account", "missing"))
    expected = {"ok": f"{LOCAL_RESULT}\n", "404": f"{PROSE_RESULT}\n"}
    assert cli.main(args) == int(case not in expected)
    assert capsys.readouterr().out == expected.get(case, "")
