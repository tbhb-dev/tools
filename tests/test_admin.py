"""CLI parsing and selected-secret repository workflow with stubbed calls."""

import io
import json
import sys
from dataclasses import replace
lazy from pathlib import Path

import httpx
import pytest
from test_admin_request import fake as admin_fake
lazy from test_admin_request import Fake

from tbhbdev import admin
lazy from tbhbdev.admin import request

fake = admin_fake


@pytest.mark.parametrize(
    ("argv", "status", "output"),
    [
        (["--help"], 0, "out"),
        ([], 2, "err"),
        (["call", "GET", "/repos/acme/widget"], 2, "err"),
        (["secret-repos", "remove"], 2, "err"),
    ],
)
def test_usage(
    argv: list[str], status: int, output: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin.main(argv, "admin") == status
    captured = capsys.readouterr()
    assert getattr(captured, output) == admin.USAGE.format(prog="admin")


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--reason", " "], "reason_required"),
        (["--reason", "test", "--vault", "../vault"], "invalid_setting"),
    ],
)
def test_invalid_options(
    fake: Fake, args: list[str], code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        admin.main(
            ["call", "GET", "/repos/acme/widget", *args], "admin", runtime=fake.runtime
        )
        == 3
    )
    assert capsys.readouterr() == ("", f"{code}\n")
    assert fake.reads == fake.requests == []


def test_default_runtime_dispatch(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "default_runtime", lambda: fake.runtime)
    fake.reply = httpx.Response(404, text="missing")
    assert (
        admin.main(["call", "GET", "/repos/acme/widget", "--reason", "test"], "admin")
        == 1
    )


@pytest.mark.parametrize("source", ["-", "file", "missing", "invalid"])
def test_body_input(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "body.json"
    if source == "-":
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"example": true}'))
        assert admin.body_from("-") == {"example": True}
    elif source == "file":
        path.write_text('{"example": true}')
        assert admin.body_from(str(path)) == {"example": True}
    else:
        if source == "invalid":
            path.write_text("invalid")
        with pytest.raises(admin.RefusedError, match=r"^invalid_body$"):
            admin.body_from(str(path))


def selected_transport(
    fake: Fake, replies: dict[str, httpx.Response]
) -> request.Runtime:
    original = fake.runtime.env.transport
    assert original is not None

    def transport(req: httpx.Request) -> httpx.Response:
        if req.url.path in replies:
            fake.reply = replies[req.url.path]
        return original.handle_request(req)

    return replace(
        fake.runtime,
        env=replace(fake.runtime.env, transport=httpx.MockTransport(transport)),
    )


def test_secret_repos_add(fake: Fake) -> None:
    runtime = selected_transport(
        fake,
        {
            "/orgs/acme/actions/secrets/build": httpx.Response(
                200, json={"visibility": "selected"}
            ),
            "/repos/acme/widget": httpx.Response(200, json={"id": 1}),
            "/repos/acme/other": httpx.Response(200, json={"id": 9}),
            "/orgs/acme/actions/secrets/build/repositories/1": httpx.Response(204),
            "/orgs/acme/actions/secrets/build/repositories/9": httpx.Response(204),
        },
    )
    assert (
        admin.main(
            [
                "secret-repos",
                "add",
                "--org",
                "acme",
                "--secret",
                "build",
                "widget",
                "acme/other",
            ],
            "admin",
            runtime=runtime,
        )
        == 0
    )
    assert [(r.method, r.url.path) for r in fake.requests[2::3]] == [
        ("GET", "/orgs/acme/actions/secrets/build"),
        ("GET", "/repos/acme/widget"),
        ("PUT", "/orgs/acme/actions/secrets/build/repositories/1"),
        ("GET", "/repos/acme/other"),
        ("PUT", "/orgs/acme/actions/secrets/build/repositories/9"),
    ]
    entries = [
        json.loads(line)
        for line in (runtime.audit.directory / "calls.jsonl").read_text().splitlines()
    ]
    assert len(entries) == 10
    assert all(
        entry["reason"] == "add repositories to selected secret" for entry in entries
    )


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"visibility": "all"}, 200),
        ({"visibility": "private"}, 200),
        ({}, 200),
        ([], 200),
        ({}, 404),
        ("invalid", 200),
    ],
)
def test_secret_visibility_refused(
    fake: Fake, payload: object, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.reply = httpx.Response(
        status, text=payload if isinstance(payload, str) else json.dumps(payload)
    )
    assert (
        admin.main(
            ["secret-repos", "add", "--org", "acme", "--secret", "build", "widget"],
            "admin",
            runtime=fake.runtime,
        )
        == 3
    )
    assert len(fake.requests) == 3
    code = (
        "secret_not_selected"
        if isinstance(payload, dict) and status == 200
        else "invalid_response"
    )
    assert capsys.readouterr() == ("", f"{code}\n")


@pytest.mark.parametrize(
    ("repo", "ident", "status", "code"),
    [
        ("other/widget", 8, 204, "repository_mismatch"),
        ("../widget", 8, 204, "repository_mismatch"),
        ("acme/a/b", 8, 204, "repository_mismatch"),
        ("widget", None, 204, "invalid_response"),
        ("widget", True, 204, "invalid_response"),
        ("widget", 0, 204, "invalid_response"),
        ("widget", -1, 204, "invalid_response"),
        ("widget", 8, 403, "request_failed"),
    ],
)
def test_repository_selection_errors(  # noqa: PLR0913, PLR0917  # Table plus fixtures.
    fake: Fake,
    repo: str,
    ident: object,
    status: int,
    code: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = selected_transport(
        fake,
        {
            "/orgs/acme/actions/secrets/build": httpx.Response(
                200, json={"visibility": "selected"}
            ),
            "/repos/acme/widget": httpx.Response(200, json={"id": ident}),
            "/orgs/acme/actions/secrets/build/repositories/1": httpx.Response(status),
        },
    )
    assert (
        admin.main(
            ["secret-repos", "add", "--org", "acme", "--secret", "build", repo],
            "admin",
            runtime=runtime,
        )
        == 3
    )
    assert capsys.readouterr() == ("", f"{code}\n")


def test_invalid_org(fake: Fake, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        admin.main(
            ["secret-repos", "add", "--org", "../acme", "--secret", "build", "widget"],
            "admin",
            runtime=fake.runtime,
        )
        == 3
    )
    assert capsys.readouterr() == ("", "ambiguous_path\n")
    assert fake.requests == fake.reads == []


def test_parser_contract() -> None:
    parser = admin.parser()
    direct = parser.parse_args(
        ["call", "PUT", "/repos/acme/widget", "--reason", "test", "--dry-run"]
    )
    assert vars(direct) == {
        "command": "call",
        "method": "PUT",
        "path": "/repos/acme/widget",
        "json": None,
        "reason": "test",
        "dry_run": True,
        "app": "tbhb-dev-admin",
        "item": "github-app-tbhb-dev-admin",
        "vault": "tbhb-dev-admin",
        "app_id": "5733271",
    }
    selected = parser.parse_args(
        ["secret-repos", "add", "--org", "acme", "--secret", "build", "widget"]
    )
    assert vars(selected) == {
        "command": "secret-repos",
        "action": "add",
        "org": "acme",
        "secret": "build",
        "repos": ["widget"],
        "reason": "add repositories to selected secret",
        "app": "tbhb-dev-admin",
        "item": "github-app-tbhb-dev-admin",
        "vault": "tbhb-dev-admin",
        "app_id": "5733271",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["secret-repos", "add", "--secret", "build", "widget"],
        ["secret-repos", "add", "--org", "acme", "widget"],
        ["secret-repos", "remove", "--org", "acme", "--secret", "build", "widget"],
    ],
)
def test_required_secret_arguments(argv: list[str]) -> None:
    with pytest.raises(admin.gh_app.UsageError):
        admin.parser().parse_args(argv)


def test_custom_settings_and_dry_run(
    fake: Fake, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[request.Settings, str, str, object, str, bool]] = []

    def called(  # noqa: PLR0913  # One-call API seam.
        runtime: request.Runtime,
        method: str,
        path: str,
        body: object,
        reason: str,
        *,
        dry_run: bool = False,
    ) -> tuple[int, str]:
        seen.append((runtime.settings, method, path, body, reason, dry_run))
        return 400, "bad request"

    monkeypatch.setattr(admin, "call", called)
    assert (
        admin.main(
            [
                "call",
                "POST",
                "/repos/acme/widget/pulls",
                "--reason",
                "maintenance",
                "--dry-run",
                "--app",
                "other-app",
                "--item",
                "other-item",
                "--vault",
                "other-vault",
                "--app-id",
                "42",
            ],
            "admin",
            runtime=fake.runtime,
        )
        == 1
    )
    assert seen == [
        (
            request.Settings("other-app", "other-item", "other-vault", "42"),
            "POST",
            "/repos/acme/widget/pulls",
            None,
            "maintenance",
            True,
        )
    ]
    assert capsys.readouterr() == ("bad request", "")
