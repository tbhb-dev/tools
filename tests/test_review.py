"""Review tiers and commit status posting."""

from __future__ import annotations

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
ROW: review.File = {
    "filename": "notes.md",
    "status": "modified",
    "additions": 1,
    "deletions": 1,
    "patch": "-old\n+new",
}


@pytest.mark.parametrize(
    "text",
    [
        "unknown = 0",
        "[tier.3]",
        "[tier.1]\npaths = 1",
        "[tier.1]\npaths = [' ']",
        "[tier.2]\npatterns = ['(']",
    ],
)
def test_bad_config(text: str) -> None:
    with pytest.raises(Invalid, match="invalid global"):
        review.load_rules(text, "global")


@pytest.mark.parametrize(
    ("field", "value", "tier"),
    [
        ("filename", "notes.md", 0),
        ("filename", "designs/notes.md", 1),
        ("filename", "script.sh", 1),
        ("filename", review.LOCAL_CONFIG, 2),
        ("previous_filename", ".github/workflows/check.yml", 2),
        ("status", "removed", 2),
        ("patch", "+truncated", 2),
        ("patch", "-rmtree\n+new", 2),
    ],
)
def test_tiers(field: str, value: str, tier: int) -> None:
    change = review.parse_change(cast("review.File", ROW | {field: value}))
    assert review.classify("wiki", [change], RULES, [])[0] == tier
    assert review.classify("tools", [change], RULES, [])[0] == max(1, tier)


@pytest.mark.parametrize("spec", ["0]\npaths=['*.md']", "1]\npaths=['*.md']"])
def test_lower_tier(spec: str) -> None:
    change = review.parse_change({**ROW, "status": "removed"})
    local = review.load_rules("[tier." + spec, "local")
    with pytest.raises(Invalid, match="would lower"):
        review.classify("wiki", [change], RULES, local)


@pytest.mark.parametrize(
    "case",
    ["ok", "404", "move", "meta", "files", "http", "none", "app", "mint", "io", "git"],
)
def test_command(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    def metadata(head: str) -> httpx.Response:
        return httpx.Response(
            200, json={"head": {"sha": head}, "base": {"sha": BASE}, "changed_files": 2}
        )

    first = metadata("" if case == "meta" else HEAD)
    rows: list[review.File] = [] if case == "files" else [ROW]
    local = httpx.Response(
        404 if case == "404" else 200, text="[tier.2]\npaths=['*.md']"
    )
    responses = iter(
        [
            first,
            httpx.Response(200, json=rows),
            httpx.Response(
                200, json=[ROW | {"filename": ".github/workflows/check.yml"}]
            ),
            local,
            metadata(BASE if case == "move" else HEAD),
            httpx.Response(403 if case == "http" else 201),
        ]
    )
    addresses = iter(
        [
            (PULL, None),
            (f"{PULL}/files", "1"),
            (f"{PULL}/files", "2"),
            (f"{ROOT}/contents/{review.LOCAL_CONFIG}", BASE),
            (PULL, None),
            (STATUS, None),
        ]
    )
    sent: dict[str, bytes] = {}

    def respond(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer fake"
        assert (
            req.url.path,
            req.url.params.get("page", req.url.params.get("ref")),
        ) == next(addresses)
        sent[req.url.path] = req.read()
        return next(responses)

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
    factory = partial(httpx.Client, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(review.httpx, "Client", factory)
    monkeypatch.setattr(review.settings, "load", pull_requests_settings)
    monkeypatch.setattr(review.credentials, "pull_requests_token", mint)
    monkeypatch.setattr(review, "PAGE_SIZE", 1)
    monkeypatch.delenv(review.CONFIG_ENV, raising=False)
    if case != "none":
        path = POLICY.parent if case == "io" else POLICY
        monkeypatch.setenv(review.CONFIG_ENV, str(path))
    args = ["review", "tier", "7"]
    if case == "app":
        args.extend(("--account", "unknown"))
    success = case in {"ok", "404"}
    assert cli.main(args) == int(not success)
    assert (STATUS in sent) == (success or case == "http")
    if success:
        reason = "global path rule 1" if case == "404" else "local path rule 1"
        assert sent[STATUS] == (
            b'{"context":"review-tier","state":"success",'
            + f'"description":"tier 2: {reason}"}}'.encode()
        )
