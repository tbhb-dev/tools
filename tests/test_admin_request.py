"""Fake Connect and GitHub only; refusal must precede both."""

# cspell:words Jheader

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
lazy from collections.abc import Callable

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tbhbdev import admin, secrets
from tbhbdev.admin import policy, request
from tbhbdev.admin.audit import Audit, RefusedError
from tbhbdev.alpha import gh_app

if TYPE_CHECKING:
    from collections.abc import Callable

TOKEN = "fake-installation-value"  # noqa: S105  # Stub credential.


@dataclass
class Fake:
    runtime: request.Runtime
    key: str = field(repr=False)
    reads: list[str]
    requests: list[httpx.Request]
    reply: httpx.Response = field(
        default_factory=lambda: httpx.Response(200, json={"ok": True})
    )
    failure: str = ""


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    reads: list[str] = []
    requests: list[httpx.Request] = []

    class Reader:
        def read(self, ref: str, /) -> str:
            reads.append(ref)
            if result.failure == "store":
                raise secrets.SecretsError(TOKEN)
            return "" if result.failure == "empty" else key

    def transport(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        # The durable before record must exist before even installation lookup.
        entries = [
            json.loads(line)
            for line in (tmp_path / "audit/calls.jsonl").read_text().splitlines()
        ]
        assert entries[-1]["phase"] == "before"
        if result.failure == "network":
            raise httpx.ReadError(TOKEN)
        if req.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 7})
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": TOKEN, "expires_at": "2030-01-01T00:00:00Z"}
            )
        if result.failure == "call":
            raise httpx.ReadError(TOKEN)
        return result.reply

    env = gh_app.Env(
        tmp_path,
        lambda: 1800000000,
        Reader,
        gh_app.run_openssl,
        httpx.MockTransport(transport),
    )
    result = Fake(request.Runtime(Audit(tmp_path / "audit"), env), key, reads, requests)
    return result


@pytest.mark.parametrize(
    ("method", "path", "body", "code"),
    [
        ("DELETE", "/repos/tbhb-dev/widget", None, "repository_removal"),
        (
            "PATCH",
            "/repos/tbhb-dev/widget",
            {"visibility": "public"},
            "public_visibility",
        ),
        ("GET", "/graphql", None, "endpoint_denied"),
    ],
)
def test_no_io_on_refusal(
    fake: Fake, method: str, path: str, body: object, code: str
) -> None:
    with pytest.raises(RefusedError, match=f"^{code}$"):
        request.call(fake.runtime, method, path, body, "test")
    assert fake.reads == []
    assert fake.requests == []
    entries = (fake.runtime.audit.directory / "calls.jsonl").read_text().splitlines()
    assert len(entries) == 1
    assert json.loads(entries[0])["decision"] == "refuse"


@pytest.mark.parametrize(
    ("method", "path", "target", "permissions"),
    [
        ("GET", "/repos/tbhb-dev/widget", "tbhb-dev/widget", {"metadata": "read"}),
        (
            "PATCH",
            "/repos/tbhb-dev/widget",
            "tbhb-dev/widget",
            {"administration": "write"},
        ),
        (
            "GET",
            "/orgs/tbhb-dev/actions/secrets/build",
            "tbhb-dev",
            {"organization_secrets": "read"},
        ),
        (
            "PUT",
            "/orgs/tbhb-dev/actions/secrets/build/repositories/8",
            "tbhb-dev",
            {"organization_secrets": "write"},
        ),
        (
            "GET",
            "/repos/tbhb-dev/widget/contents/file",
            "tbhb-dev/widget",
            {"contents": "read"},
        ),
        (
            "PUT",
            "/repos/tbhb-dev/widget/environments/test",
            "tbhb-dev/widget",
            {"environments": "write"},
        ),
        (
            "POST",
            "/repos/tbhb-dev/widget/pulls",
            "tbhb-dev/widget",
            {"pull_requests": "write"},
        ),
    ],
)
def test_scope_and_real_mint(
    fake: Fake, method: str, path: str, target: str, permissions: dict[str, str]
) -> None:
    decision = policy.check(method, path)
    assert request.scope(decision, method) == (target, permissions)
    assert request.call(fake.runtime, method, path, None, "test") == (
        200,
        '{"ok":true}',
    )
    assert fake.reads == ["op://tbhb-dev-admin/github-app-tbhb-dev-admin/private_key"]
    assert fake.requests[0].url.path == (
        f"/repos/{target}/installation"
        if "/" in target
        else f"/orgs/{target}/installation"
    )
    expected: dict[str, object] = {"permissions": permissions}
    if "/" in target:
        expected["repositories"] = ["widget"]
    assert json.loads(fake.requests[1].content) == expected
    jwt = fake.requests[0].headers["authorization"].split()[1]
    assert (
        json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))["iss"]
        == "5733271"
    )
    assert fake.requests[2].headers["authorization"] == f"Bearer {TOKEN}"
    assert fake.requests[2].url.path == path
    assert fake.requests[2].method == method
    assert fake.requests[2].content == b""
    request.call(fake.runtime, method, path, None, "again")
    assert len(fake.reads) == 2
    assert len(fake.requests) == 6
    text = (fake.runtime.audit.directory / "calls.jsonl").read_text()
    assert TOKEN not in text
    assert fake.key not in text
    entries = [json.loads(line) for line in text.splitlines()]
    assert [r["phase"] for r in entries] == ["before", "after", "before", "after"]
    assert entries[1]["status"] == 200
    assert entries[0]["status"] is None
    assert entries[0]["method"] == method
    assert entries[0]["reason"] == "test"
    assert len({r["id"] for r in entries}) == 2
    assert repr(fake.runtime).find(fake.key) == -1


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("store", "credential_unavailable"),
        ("empty", "credential_unavailable"),
        ("network", "request_failed"),
        ("call", "request_failed"),
    ],
)
def test_failures_are_safe(
    fake: Fake, capsys: pytest.CaptureFixture[str], failure: str, code: str
) -> None:
    fake.failure = failure
    assert (
        admin.main(
            ["call", "GET", "/repos/tbhb-dev/widget", "--reason", "test"],
            "admin",
            runtime=fake.runtime,
        )
        == 3
    )
    assert capsys.readouterr() == ("", f"{code}\n")
    text = (fake.runtime.audit.directory / "calls.jsonl").read_text()
    assert TOKEN not in text
    assert fake.key not in text
    entry = json.loads(text.splitlines()[-1])
    assert entry["code"] == code
    assert entry["phase"] == "after"
    assert entry["status"] is None


def test_dry_run(fake: Fake) -> None:
    assert request.call(
        fake.runtime, "GET", "/repos/tbhb-dev/widget", None, "test", dry_run=True
    ) == (0, "")
    assert fake.reads == fake.requests == []


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (
            httpx.Response(302, headers={"Location": "https://example.com"}),
            "redirect_refused",
        ),
        (
            httpx.Response(200, content=b"x" * (request.MAX_RESPONSE + 1)),
            "response_too_large",
        ),
    ],
)
def test_response_limits(fake: Fake, reply: httpx.Response, code: str) -> None:
    fake.reply = reply
    with pytest.raises(RefusedError, match=f"^{code}$"):
        request.call(fake.runtime, "GET", "/repos/tbhb-dev/widget", None, "test")
    assert len(fake.requests) == 3


def test_redaction_and_json_body(
    fake: Fake, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake.reply = httpx.Response(
        200, text=f"{TOKEN} {fake.key} eyJheader.payload.signature"
    )
    source = tmp_path / "body.json"
    source.write_text('{"has_wiki": false, "description": "example"}')
    assert (
        admin.main(
            [
                "call",
                "PATCH",
                "/repos/tbhb-dev/widget",
                "--json",
                str(source),
                "--reason",
                "test",
            ],
            "admin",
            runtime=fake.runtime,
        )
        == 0
    )
    assert capsys.readouterr() == ("[redacted] [redacted] [redacted]", "")
    assert fake.requests[-1].content == b'{"description":"example","has_wiki":false}'
    assert fake.key not in repr(fake)


def test_response_cap_boundary(fake: Fake) -> None:
    fake.reply = httpx.Response(200, content=b"x" * request.MAX_RESPONSE)
    assert (
        len(
            request.call(fake.runtime, "GET", "/repos/tbhb-dev/widget", None, "test")[1]
        )
        == request.MAX_RESPONSE
    )


def test_default_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    runtime = request.default_runtime()
    assert isinstance(runtime.env, gh_app.Env)
    assert runtime.audit.directory == tmp_path / "tbhbdev/admin"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert (
        request.default_runtime().audit.directory
        == tmp_path / ".local/state/tbhbdev/admin"
    )


def test_explicit_http_options(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:

    original: Callable[..., httpx.Client] = httpx.Client
    options: list[dict[str, object]] = []

    def client(**kwargs: object) -> httpx.Client:
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    request.call(fake.runtime, "GET", "/repos/tbhb-dev/widget", None, "test")
    last = options[-1]
    assert last["timeout"] == gh_app.TIMEOUT
    assert last["follow_redirects"] is False
    assert last["trust_env"] is False
    assert last["base_url"] == "https://api.github.com"
    assert last["transport"] is fake.runtime.env.transport
    assert cast("dict[str, str]", last["headers"]) == {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def test_redaction_without_pem_structure() -> None:
    assert (
        request.redact("fake-value fake-credential", "fake-value", "fake-credential")
        == "[redacted] [redacted]"
    )


def test_invalid_utf8_response(fake: Fake) -> None:
    fake.reply = httpx.Response(200, content=b"\xff")
    assert request.call(
        fake.runtime, "GET", "/repos/tbhb-dev/widget", None, "test"
    ) == (
        200,
        "\ufffd",
    )


def test_reused_mint_preserves_installation_arguments(fake: Fake) -> None:
    fake.runtime.audit.append({"phase": "before"})
    found: list[int] = []
    minted = gh_app.mint_token(
        fake.runtime.env,
        app_id="5733271",
        pem=fake.key,
        repository="tbhb-dev/widget",
        permissions={"administration": "write"},
        known_installation=7,
        installation_found=found.append,
    )
    assert minted.token == TOKEN
    assert found == [7]
    assert len(fake.requests) == 1
    assert fake.requests[0].url.path == "/app/installations/7/access_tokens"
