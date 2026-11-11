"""Tests for tbhbdev.secrets, against fake Connect APIs on 127.0.0.1.

The macOS keychain tests run a stub security executable with HOME in
tmp_path (see conftest.py); the Linux token file tests use a real file under a
test home.
"""

from __future__ import annotations

import functools
import gc
import json
import os
import socket
import stat
import subprocess
import traceback
from typing import TYPE_CHECKING, override
lazy from pathlib import Path

import httpx
import pytest
from onepasswordconnectsdk.client import Client

from tbhbdev import secrets

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import FakeConnect, Installed, Json, Stubs
    from onepasswordconnectsdk.config import ClientConfig

VALUE = "plain-value-never-shown"
# A tailnet suffix, which no URL may carry into a message.
SUFFIX = "example.ts.net"


def closed_url() -> str:
    """A loopback URL nothing listens on, so connecting is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def fake(connect: Callable[[], FakeConnect]) -> FakeConnect:
    """One Connect API with an `ops` vault holding a `db` item."""
    server = connect()
    ops = server.vault("ops")
    server.vault("spare")
    server.item(ops, "db", password=VALUE, user="admin")
    return server


def store(fake: FakeConnect) -> secrets.Store:
    return secrets.Store(secrets.Connect(fake.token, [fake.url]))


# References and configuration.


@pytest.mark.parametrize(
    ("ref", "parts"),
    [
        ("ops/db/password", ("ops", "db", "password")),
        ("op://ops/db/password", ("ops", "db", "password")),
    ],
)
def test_parse_ref_takes_both_forms(ref: str, parts: tuple[str, str, str]) -> None:
    assert secrets.parse_ref(ref) == parts


@pytest.mark.parametrize(
    "ref", ["ops/db", "ops/db/section/password", "ops//password", "op://", ""]
)
def test_parse_ref_rejects_other_shapes(ref: str) -> None:
    with pytest.raises(secrets.SecretsError, match="not a vault/item/field"):
        secrets.parse_ref(ref)


def test_endpoints_come_from_the_user_config(
    config: Callable[[list[str]], None],
) -> None:
    config(["https://connect-a.example.ts.net", "https://connect-b.example.ts.net"])
    assert secrets.endpoints() == [
        "https://connect-a.example.ts.net",
        "https://connect-b.example.ts.net",
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (None, "doesn't exist; create it and set [connect] endpoints"),
        ("[connect\n", "cannot parse "),
        ("", "sets no [connect] endpoints"),
        ("connect = 1\n", "sets no [connect] endpoints"),
        ("[connect]\nendpoints = []\n", "[connect] endpoints must be a non-empty"),
        ('[connect]\nendpoints = "https://a"\n', "endpoints must be a non-empty"),
    ],
)
def test_endpoints_rejects_a_bad_config(
    config_file: Path, text: str | None, error: str
) -> None:
    if text is not None:
        config_file.write_text(text)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.endpoints()
    assert str(config_file) in str(caught.value)
    assert error in str(caught.value)
    assert str(caught.value).endswith("; run `tbhbdev host bootstrap` for this host")


def test_the_missing_endpoints_point_at_bootstrap(config_file: Path) -> None:
    config_file.write_text('[connect]\nserver_hosts = ["nas-a"]\n')
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.endpoints()
    assert str(caught.value) == (
        f"{config_file} sets no [connect] endpoints; "
        "run `tbhbdev host bootstrap` for this host"
    )


def test_a_plugin_config_is_not_read(installed: Installed) -> None:
    installed.stub("stub", files={"secrets.toml": '[connect]\nendpoints = ["x"]\n'})
    with pytest.raises(secrets.SecretsError, match="doesn't exist"):
        secrets.endpoints()


# The token in the keychain.


def test_token_override_skips_the_keychain(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secrets.TEST_TOKEN_ENV, "fake-override")
    assert secrets.token() == "fake-override"
    assert stubs.calls() == []


def mac_keychain(stubs: Stubs, home: Path, value: str | None) -> None:
    """A macOS agent keychain under HOME, holding VALUE when it isn't None."""
    path = home / "Library" / "Keychains" / f"{secrets.KEYCHAIN}-db"
    path.parent.mkdir(parents=True)
    items: dict[str, str] = {}
    if value is not None:
        items[f"{secrets.SERVICE}/{secrets.ACCOUNT}"] = value
    path.write_text(json.dumps({"password": "", "locked": True, "items": items}))
    assert stubs.calls() == []


def test_token_on_macos_unlocks_then_reads_the_agent_keychain(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    mac_keychain(stubs, tmp_path / "home", "fake-mac-token")
    assert secrets.token() == "fake-mac-token"
    assert stubs.argvs("security") == [
        ["unlock-keychain", "-p", "", secrets.KEYCHAIN],
        [
            "find-generic-password",
            "-s",
            secrets.SERVICE,
            "-a",
            secrets.ACCOUNT,
            "-w",
            secrets.KEYCHAIN,
        ],
    ]


@pytest.mark.parametrize("make", [True, False])
def test_token_on_macos_without_an_entry_fails(
    stubs: Stubs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    make: bool,
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    if make:
        mac_keychain(stubs, tmp_path / "home", None)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == (
        "no Connect token in this user's keychain; "
        "run `tbhbdev host bootstrap` for this host"
    )
    # Not even security's own complaint about a missing keychain.
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("value", ["fake-token ", "fake-tokenX"])
def test_token_strips_only_the_newline_security_adds(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    mac_keychain(stubs, tmp_path / "home", value)
    assert secrets.token() == value


HINT = "have configuration management install it"


@pytest.fixture
def linux_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home for a Linux user, with a private secrets directory."""
    monkeypatch.setattr(secrets.platform, "system", lambda: "Linux")
    monkeypatch.delenv(secrets.TEST_TOKEN_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / ".config" / "tbhbdev" / "secrets").mkdir(parents=True)
    (tmp_path / ".config" / "tbhbdev" / "secrets").chmod(0o700)
    return tmp_path


def token_file(home: Path, text: str | None = "fake-linux-token\n") -> Path:
    path = home / ".config" / "tbhbdev" / "secrets" / "connect-token"
    if text is not None:
        path.write_text(text)
        path.chmod(0o600)
    return path


def test_the_linux_token_path_is_under_home() -> None:
    assert (
        secrets.LINUX_TOKEN_FILE.as_posix() == ".config/tbhbdev/secrets/connect-token"
    )


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("fake-linux-token\n", "fake-linux-token"),
        ("fake-linux-token", "fake-linux-token"),
        ("fake-linux-token\n\n", "fake-linux-token"),
        ("fake-linux-token ", "fake-linux-token"),
        (" \t\r\nfake-linux-token \r\n", "fake-linux-token"),
    ],
)
def test_token_on_linux_reads_the_file(linux_home: Path, text: str, value: str) -> None:
    token_file(linux_home, text)
    assert secrets.token() == value


def test_token_on_linux_ignores_xdg_config_home(linux_home: Path) -> None:
    token_file(linux_home)
    other = linux_home / "xdg" / "tbhbdev" / "secrets"
    other.mkdir(parents=True)
    (other / "connect-token").write_text("wrong")
    assert secrets.token() == "fake-linux-token"


def refused(path: Path, problem: str) -> str:
    return f"the Connect token file {path} {problem}; {HINT}"


def test_token_on_linux_without_a_file_fails(linux_home: Path) -> None:
    path = token_file(linux_home, None)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == refused(
        path, "can't be opened: No such file or directory"
    )


def test_token_on_linux_without_a_directory_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Linux")
    monkeypatch.delenv(secrets.TEST_TOKEN_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(secrets.SecretsError, match="can't be opened"):
        secrets.token()


@pytest.mark.parametrize("text", ["", "\n", " \t\r\n"])
def test_token_on_linux_with_an_empty_file_fails(linux_home: Path, text: str) -> None:
    path = token_file(linux_home, text)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == refused(path, "is empty")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o602, 0o610, 0o601])
def test_token_on_linux_refuses_a_file_open_to_others(
    linux_home: Path, mode: int
) -> None:
    path = token_file(linux_home)
    path.chmod(mode)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == refused(path, "is accessible to its group or others")
    assert "fake-linux-token" not in str(caught.value)


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770, 0o701, 0o710, 0o702])
def test_token_on_linux_refuses_a_directory_open_to_others(
    linux_home: Path, mode: int
) -> None:
    path = token_file(linux_home)
    path.parent.chmod(mode)
    try:
        with pytest.raises(secrets.SecretsError) as caught:
            secrets.token()
    finally:
        path.parent.chmod(0o700)
    assert str(caught.value) == (
        f"the Connect token directory {path.parent} is unsafe; {HINT}"
    )


def test_token_on_linux_refuses_a_symlink(linux_home: Path) -> None:
    path = token_file(linux_home, None)
    real = linux_home / "real-token"
    real.write_text("fake-linux-token\n")
    real.chmod(0o600)
    path.symlink_to(real)
    with pytest.raises(secrets.SecretsError, match="can't be opened") as caught:
        secrets.token()
    assert "fake-linux-token" not in str(caught.value)


def test_token_on_linux_refuses_a_symlinked_directory(linux_home: Path) -> None:
    path = token_file(linux_home)
    moved = linux_home / "moved"
    path.parent.rename(moved)
    path.parent.symlink_to(moved)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert "can't be opened" in str(caught.value)


def test_token_on_linux_refuses_a_file_that_is_not_regular(linux_home: Path) -> None:
    path = token_file(linux_home, None)
    path.mkdir()
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == refused(path, "isn't a regular file")


def test_token_on_linux_refuses_a_file_another_user_owns(
    linux_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = token_file(linux_home)
    real_stat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        found = real_stat(fd)
        values = list(found)
        if stat.S_ISREG(found.st_mode):
            values[4] += 1
        return os.stat_result(values)

    monkeypatch.setattr(secrets.os, "fstat", fstat)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == refused(path, "isn't owned by this user")


def test_token_on_linux_refuses_a_directory_another_user_owns(
    linux_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = token_file(linux_home)
    me = os.geteuid()
    real_stat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        found = real_stat(fd)
        if stat.S_ISDIR(found.st_mode):
            values = list(found)
            values[4] = me + 1
            return os.stat_result(values)
        return found

    monkeypatch.setattr(secrets.os, "fstat", fstat)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == (
        f"the Connect token directory {path.parent} is unsafe; {HINT}"
    )


def test_token_on_linux_refuses_a_fifo(linux_home: Path) -> None:
    path = token_file(linux_home, None)
    os.mkfifo(path, 0o600)
    with pytest.raises(secrets.SecretsError, match="isn't a regular file"):
        secrets.token()


def test_token_without_the_keychain_tool_fails(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(secrets.host_tools, "search", lambda: str(tmp_path / "empty"))
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == (
        "can't read the Connect token: security isn't installed"
    )
    assert stubs.calls() == []


@pytest.fixture
def resolved_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    executable = str(tmp_path / "host-bin" / "security")

    def resolve(name: str) -> str:
        assert name == "security"
        return executable

    monkeypatch.setattr(secrets.host_tools, "resolve", resolve)
    return executable


@pytest.mark.parametrize("command", ["unlock-keychain", "find-generic-password"])
def test_keychain_timeout(
    monkeypatch: pytest.MonkeyPatch, command: str, resolved_security: str
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    monkeypatch.delenv(secrets.TEST_TOKEN_ENV, raising=False)
    commands: list[str] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == resolved_security
        assert kwargs["timeout"] == secrets.KEYCHAIN_TIMEOUT == 10.0
        commands.append(argv[1])
        if argv[1] == command:
            raise subprocess.TimeoutExpired(
                argv,
                secrets.KEYCHAIN_TIMEOUT,
                output="stdout-marker",
                stderr="stderr-marker",
            )
        return subprocess.CompletedProcess(argv, 0, "fake-token\n", "")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == "the keychain read timed out after 10.0 seconds"
    assert commands[-1] == command
    message = "".join(traceback.format_exception(caught.value))
    assert "stdout-marker" not in message
    assert "stderr-marker" not in message


def fake_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every command token runs, each printing a fake token and succeeding."""
    commands: list[list[str]] = []

    def run(argv: list[str], **_: bool) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "fake-token\n", "")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    monkeypatch.delenv(secrets.TEST_TOKEN_ENV, raising=False)
    return commands


# macOS finds a stub tool whatever the case of its name, so these check the
# names token runs without the stubs.


def test_token_on_macos_uses_resolved_security(
    monkeypatch: pytest.MonkeyPatch, resolved_security: str
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    commands = fake_runs(monkeypatch)
    assert secrets.token() == "fake-token"
    assert [argv[:2] for argv in commands] == [
        [resolved_security, "unlock-keychain"],
        [resolved_security, "find-generic-password"],
    ]


# Failover.


@pytest.mark.parametrize("status", [500, 503])
def test_failover_skips_a_5xx_and_a_refused_endpoint(
    connect: Callable[[], FakeConnect], fake: FakeConnect, status: int
) -> None:
    broken = connect()
    broken.status = status
    backend = secrets.Connect(fake.token, [broken.url, closed_url(), fake.url])
    assert secrets.Store(backend).read("ops/db/password") == VALUE
    assert broken.requests == [("GET", "/v1/vaults")]
    # The endpoint that answered is tried first from then on.
    assert secrets.Store(backend).read("ops/db/user") == "admin"
    assert broken.requests == [("GET", "/v1/vaults")]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_4xx_is_final(
    connect: Callable[[], FakeConnect], fake: FakeConnect, status: int
) -> None:
    refusing = connect()
    refusing.status = status
    backend = secrets.Connect(fake.token, [refusing.url, fake.url])
    with pytest.raises(secrets.SecretsError, match=f"HTTP {status}"):
        backend.vaults()
    assert fake.requests == []


def test_no_endpoint_answering_fails(connect: Callable[[], FakeConnect]) -> None:
    down = connect()
    down.status = 502
    backend = secrets.Connect(down.token, [closed_url(), down.url])
    with pytest.raises(secrets.SecretsError, match="no Connect endpoint answered"):
        backend.vaults()


# The endpoint that fails_at makes fail before it sends a request.
FIRST = f"https://nas-a.{SUFFIX}"
POSTED = ("POST", "/v1/vaults/v1/items")


def fail_at(monkeypatch: pytest.MonkeyPatch, error: type[Exception]) -> None:
    """Make every request to FIRST raise `error`."""

    class Failing(Client):
        @override
        def build_request(
            self, method: str, path: str, body: object = None
        ) -> httpx.Response:
            if self.url == FIRST:
                message = "fake"
                raise error(message)
            return super().build_request(method, path, body)

    monkeypatch.setattr(secrets, "Client", Failing)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_a_post_moves_on_when_it_cant_connect(
    monkeypatch: pytest.MonkeyPatch, fake: FakeConnect, error: type[Exception]
) -> None:
    fail_at(monkeypatch, error)
    backend = secrets.Connect(fake.token, [FIRST, fake.url])
    assert backend.create("v1", {"title": "x"})["title"] == "x"
    assert fake.requests == [POSTED]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_a_post_an_endpoint_may_have_done_is_final(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeConnect,
    error: type[Exception],
) -> None:
    fail_at(monkeypatch, error)
    backend = secrets.Connect(fake.token, [FIRST, fake.url])
    with pytest.raises(secrets.SecretsError) as caught:
        backend.create("v1", {"title": "x"})
    assert str(caught.value) == (
        f"Connect may have done POST /v1/vaults/v1/items: {error.__name__}"
    )
    assert fake.requests == []


def test_a_post_with_a_5xx_is_final(
    connect: Callable[[], FakeConnect], fake: FakeConnect
) -> None:
    broken = connect()
    broken.status = 500
    backend = secrets.Connect(fake.token, [broken.url, fake.url])
    with pytest.raises(secrets.SecretsError) as caught:
        backend.create("v1", {"title": "x"})
    assert str(caught.value) == "Connect failed POST /v1/vaults/v1/items: HTTP 500"
    assert broken.requests == [POSTED]
    assert fake.requests == []


def first_fails(
    how: str, monkeypatch: pytest.MonkeyPatch, connect: Callable[[], FakeConnect]
) -> str:
    """The URL of an endpoint that times out reading or answers HTTP 503."""
    if how == "timeout":
        fail_at(monkeypatch, httpx.ReadTimeout)
        return FIRST
    broken = connect()
    broken.status = 503
    return broken.url


@pytest.mark.parametrize("how", ["timeout", "5xx"])
def test_a_put_or_delete_moves_on_when_an_endpoint_may_have_done_it(
    monkeypatch: pytest.MonkeyPatch,
    connect: Callable[[], FakeConnect],
    fake: FakeConnect,
    how: str,
) -> None:
    first = first_fails(how, monkeypatch, connect)
    backend = secrets.Connect(fake.token, [first, fake.url])
    assert backend.update("v1", "i1", {"title": "x"})["title"] == "x"
    backend.delete("v1", "i1")
    assert fake.requests == [
        ("PUT", "/v1/vaults/v1/items/i1"),
        ("DELETE", "/v1/vaults/v1/items/i1"),
    ]
    assert fake.items["v1"] == {}


@pytest.mark.parametrize("how", ["timeout", "5xx"])
def test_a_delete_that_finds_nothing_after_moving_on_has_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    connect: Callable[[], FakeConnect],
    fake: FakeConnect,
    how: str,
) -> None:
    first = first_fails(how, monkeypatch, connect)
    backend = secrets.Connect(fake.token, [first, fake.url])
    backend.delete("v1", "i9")
    assert fake.requests == [("DELETE", "/v1/vaults/v1/items/i9")]


def test_a_delete_that_finds_nothing_after_a_refused_connection_fails(
    fake: FakeConnect,
) -> None:
    backend = secrets.Connect(fake.token, [closed_url(), fake.url])
    with pytest.raises(secrets.SecretsError) as caught:
        backend.delete("v1", "i9")
    assert (
        str(caught.value) == "Connect refused DELETE /v1/vaults/v1/items/i9: HTTP 404"
    )


@pytest.mark.parametrize(
    ("call", "sent", "status"),
    [
        (
            functools.partial(
                secrets.Connect.update, vault_id="v1", item_id="i9", item={}
            ),
            "PUT /v1/vaults/v1/items/i9",
            404,
        ),
        (
            functools.partial(secrets.Connect.delete, vault_id="v9", item_id="i1"),
            "DELETE /v1/vaults/v9/items/i1",
            403,
        ),
    ],
)
def test_only_a_delete_that_finds_nothing_after_moving_on_succeeds(
    connect: Callable[[], FakeConnect],
    fake: FakeConnect,
    call: Callable[[secrets.Connect], object],
    sent: str,
    status: int,
) -> None:
    broken = connect()
    broken.status = 503
    with pytest.raises(secrets.SecretsError) as caught:
        call(secrets.Connect(fake.token, [broken.url, fake.url]))
    assert str(caught.value) == f"Connect refused {sent}: HTTP {status}"


@pytest.mark.parametrize(
    ("call", "sent"),
    [
        (
            functools.partial(secrets.Connect.items, vault_id="v1"),
            "GET /v1/vaults/v1/items",
        ),
        (
            functools.partial(secrets.Connect.item, vault_id="v1", item_id="i1"),
            "GET /v1/vaults/v1/items/i1",
        ),
        (
            functools.partial(
                secrets.Connect.create, vault_id="v1", item={"title": "x"}
            ),
            "POST /v1/vaults/v1/items",
        ),
        (
            functools.partial(
                secrets.Connect.update, vault_id="v1", item_id="i1", item={"title": "x"}
            ),
            "PUT /v1/vaults/v1/items/i1",
        ),
        (
            functools.partial(secrets.Connect.delete, vault_id="v1", item_id="i1"),
            "DELETE /v1/vaults/v1/items/i1",
        ),
    ],
)
def test_a_refusal_names_the_request(
    fake: FakeConnect, call: Callable[[secrets.Connect], object], sent: str
) -> None:
    fake.status = 404
    with pytest.raises(secrets.SecretsError) as caught:
        call(secrets.Connect(fake.token, [fake.url]))
    assert str(caught.value) == f"Connect refused {sent}: HTTP 404"


@pytest.mark.parametrize(
    ("raw", "call", "error"),
    [
        (
            b"<html>",
            secrets.Connect.vaults,
            "Connect sent an unexpected response to GET /v1/vaults",
        ),
        (b"{}", secrets.Connect.vaults, "Connect sent an unexpected response"),
        (
            b"[]",
            functools.partial(secrets.Connect.item, vault_id="v1", item_id="i1"),
            "Connect sent an unexpected response",
        ),
    ],
)
def test_an_unexpected_answer_fails(
    fake: FakeConnect, raw: bytes, call: Callable[[secrets.Connect], object], error: str
) -> None:
    fake.raw = raw
    backend = secrets.Connect(fake.token, [fake.url])
    with pytest.raises(secrets.SecretsError) as caught:
        call(backend)
    assert str(caught.value) == error


def test_each_client_has_the_timeout(
    fake: FakeConnect, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[Client] = []

    class Recording(Client):
        def __init__(
            self, url: str, token: str, config: ClientConfig | None = None
        ) -> None:
            super().__init__(url, token, config)
            made.append(self)

    monkeypatch.setattr(secrets, "Client", Recording)
    secrets.Connect(fake.token, [fake.url]).vaults()
    assert [client.session.timeout for client in made] == [secrets.TIMEOUT]


def test_connect_repr_leaves_out_the_token(fake: FakeConnect) -> None:
    assert fake.token not in repr(secrets.Connect(fake.token, [fake.url]))


def assert_url_unseen(
    caught: pytest.ExceptionInfo[secrets.SecretsError],
    backend: secrets.Connect,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The suffix is in no message, traceback, repr or output."""
    shown = "".join(traceback.format_exception(caught.value))
    captured = capfd.readouterr()
    for text in (str(caught.value), shown, repr(backend), captured.out, captured.err):
        assert SUFFIX not in text
    assert repr(backend) == "Connect(endpoints=1)"


@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize(
    ("url", "kind"),
    [
        # httpx refuses the URL when it builds the client, and names its host.
        (f"https://[nas-a.{SUFFIX}]", "InvalidURL"),
        # idna refuses the host when httpx builds the request.
        (f"https://xn--zz.{SUFFIX}", "IDNAError"),
    ],
)
def test_a_url_that_cant_be_sent_stays_out_of_the_error(
    capfd: pytest.CaptureFixture[str], url: str, kind: str
) -> None:
    backend = secrets.Connect("a-token", [url])
    with pytest.raises(secrets.SecretsError) as caught:
        backend.vaults()
    assert str(caught.value) == f"Connect can't send GET /v1/vaults: {kind}"
    assert_url_unseen(caught, backend, capfd)
    # A half-made client would fail as it's deleted, here.
    del caught, backend
    gc.collect()


@pytest.mark.parametrize(
    "error",
    [httpx.DecodingError, httpx.TooManyRedirects, httpx.InvalidURL, httpx.StreamError],
)
def test_any_other_httpx_error_is_final_and_hides_the_url(
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    class Failing(Client):
        @override
        def build_request(
            self, method: str, path: str, body: object = None
        ) -> httpx.Response:
            del method, path, body
            message = f"cannot reach {self.url}"
            raise error(message)

    monkeypatch.setattr(secrets, "Client", Failing)
    backend = secrets.Connect("a-token", [f"https://nas-a.{SUFFIX}"])
    with pytest.raises(secrets.SecretsError) as caught:
        backend.item("v1", "i1")
    assert str(caught.value) == (
        f"Connect can't send GET /v1/vaults/v1/items/i1: {error.__name__}"
    )
    assert_url_unseen(caught, backend, capfd)


# Reading and getting.


@pytest.mark.parametrize(
    "ref", ["ops/db/password", "op://ops/db/password", "v1/i1/f0", "op://v1/db/f0"]
)
def test_read_by_name_or_id(fake: FakeConnect, ref: str) -> None:
    assert store(fake).read(ref) == VALUE


def test_read_a_field_without_a_value_is_empty(fake: FakeConnect) -> None:
    fields = fake.items["v1"]["i1"]["fields"]
    assert isinstance(fields, list)
    fields.append({"id": "f9", "label": "empty", "type": "STRING"})
    assert store(fake).read("ops/db/empty") == ""


def test_read_an_item_without_fields_fails(fake: FakeConnect) -> None:
    del fake.items["v1"]["i1"]["fields"]
    with pytest.raises(secrets.SecretsError, match="no field 'password'"):
        store(fake).read("ops/db/password")


@pytest.mark.parametrize(
    ("ref", "error"),
    [
        ("nope/db/password", "no vault 'nope'"),
        ("ops/nope/password", "no item 'nope' in vault 'ops'"),
        ("ops/db/nope", "no field 'nope' in item 'db'"),
    ],
)
def test_read_something_missing_fails(fake: FakeConnect, ref: str, error: str) -> None:
    with pytest.raises(secrets.SecretsError, match=error) as caught:
        store(fake).read(ref)
    assert VALUE not in str(caught.value)


def test_read_an_ambiguous_title_fails(fake: FakeConnect) -> None:
    fake.item("v1", "db", password="other")  # noqa: S106
    with pytest.raises(secrets.SecretsError, match="more than one item 'db'"):
        store(fake).read("ops/db/password")


def test_module_read_uses_the_keychain_token_and_config(
    fake: FakeConnect,
    config: Callable[[list[str]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config([fake.url])
    monkeypatch.setenv(secrets.TEST_TOKEN_ENV, fake.token)
    assert secrets.read("ops/db/password") == VALUE


def test_get_returns_metadata_and_fields_without_values_in_repr(
    fake: FakeConnect,
) -> None:
    item = store(fake).get("ops", "db")
    assert (item.id, item.title, item.category, item.vault_id, item.vault) == (
        "i1",
        "db",
        "LOGIN",
        "v1",
        "ops",
    )
    assert [(f.id, f.label, f.type, f.value) for f in item.fields] == [
        ("f0", "password", "CONCEALED", VALUE),
        ("f1", "user", "CONCEALED", "admin"),
    ]
    assert VALUE not in repr(item)


def test_list_items_in_one_vault_or_all(fake: FakeConnect) -> None:
    fake.item("v2", "spare-key", key="k")
    one = store(fake).list_items("ops")
    assert [(s.vault, s.title) for s in one] == [("ops", "db")]
    every = store(fake).list_items()
    assert [(s.vault, s.title, s.id) for s in every] == [
        ("ops", "db", "i1"),
        ("spare", "spare-key", "i2"),
    ]


# Writing.


def test_create_makes_a_secure_note_of_concealed_fields(fake: FakeConnect) -> None:
    item = store(fake).create("spare", "api", {"key": "k1", "secret": "k2"})
    stored = fake.items["v2"][item.id]
    assert stored["category"] == "SECURE_NOTE"
    assert stored["vault"] == {"id": "v2"}
    assert stored["fields"] == [
        {"label": "key", "value": "k1", "type": "CONCEALED"},
        {"label": "secret", "value": "k2", "type": "CONCEALED"},
    ]
    assert (item.vault_id, item.vault) == ("v2", "spare")


def test_create_refuses_a_title_the_vault_has(fake: FakeConnect) -> None:
    with pytest.raises(secrets.SecretsError, match="vault 'ops' already has an item"):
        store(fake).create("ops", "db", {"x": "y"})


def test_edit_sets_existing_fields_adds_new_ones_and_keeps_the_rest(
    fake: FakeConnect,
) -> None:
    fake.items["v1"]["i1"]["urls"] = [{"href": "https://db"}]
    item = store(fake).edit("ops", "db", {"password": "new", "port": "5432"})
    assert (item.vault_id, item.vault) == ("v1", "ops")
    stored = fake.items["v1"]["i1"]
    assert stored["urls"] == [{"href": "https://db"}]
    assert stored["fields"] == [
        {"id": "f0", "label": "password", "type": "CONCEALED", "value": "new"},
        {"id": "f1", "label": "user", "type": "CONCEALED", "value": "admin"},
        {"label": "port", "value": "5432", "type": "CONCEALED"},
    ]


def test_edit_an_ambiguous_label_fails_without_writing(fake: FakeConnect) -> None:
    fields = fake.items["v1"]["i1"]["fields"]
    assert isinstance(fields, list)
    fields.append({"id": "f9", "label": "password", "type": "CONCEALED", "value": "x"})
    with pytest.raises(secrets.SecretsError, match="more than one field labeled"):
        store(fake).edit("ops", "db", {"password": "new"})
    assert ("PUT", "/v1/vaults/v1/items/i1") not in fake.requests


def test_delete_removes_the_item(fake: FakeConnect) -> None:
    store(fake).delete("ops", "db")
    assert fake.items["v1"] == {}


@pytest.mark.parametrize(
    "call",
    [
        functools.partial(secrets.Store.get, vault="ops", item="nope"),
        functools.partial(secrets.Store.edit, vault="ops", item="nope", fields={}),
        functools.partial(secrets.Store.delete, vault="ops", item="nope"),
        functools.partial(
            secrets.Store.move, vault="ops", item="nope", destination="spare"
        ),
    ],
)
def test_a_missing_item_names_its_vault(
    fake: FakeConnect, call: Callable[[secrets.Store], object]
) -> None:
    with pytest.raises(secrets.SecretsError) as caught:
        call(store(fake))
    assert str(caught.value) == "no item 'nope' in vault 'ops'"


# Moving.


def test_move_copies_then_deletes(fake: FakeConnect) -> None:
    fake.items["v1"]["i1"]["tags"] = ["db"]
    moved = store(fake).move("ops", "db", "spare")
    assert fake.items["v1"] == {}
    copy = fake.items["v2"][moved.id]
    assert copy["vault"] == {"id": "v2"}
    assert copy["tags"] == ["db"]
    assert "version" not in copy
    assert (moved.vault_id, moved.vault) == ("v2", "spare")
    assert [f.value for f in moved.fields] == [VALUE, "admin"]


@pytest.mark.parametrize(
    ("fail", "fail_after"),
    [
        ({"POST": 500}, {}),
        ({"POST": 400}, {}),
        ({}, {"POST": 500}),
        ({}, {"POST": 409}),
        ({"DELETE /v1/vaults/v1/items/i1": 403}, {}),
        ({"DELETE /v1/vaults/v1/items/i1": 503}, {}),
    ],
)
def test_a_failed_move_leaves_nothing_behind(
    fake: FakeConnect, fail: dict[str, int], fail_after: dict[str, int]
) -> None:
    before = json.dumps(fake.items["v1"])
    fake.fail, fake.fail_after = fail, fail_after
    with pytest.raises(secrets.SecretsError) as caught:
        store(fake).move("ops", "db", "spare")
    assert fake.items["v2"] == {}
    assert json.dumps(fake.items["v1"]) == before
    assert VALUE not in str(caught.value)


def test_a_delete_that_fails_after_deleting_keeps_the_copy(
    fake: FakeConnect,
) -> None:
    fake.fail_after = {"DELETE": 500}
    moved = store(fake).move("ops", "db", "spare")
    assert fake.items["v1"] == {}
    assert fake.items["v2"][moved.id]["title"] == "db"


def test_a_title_like_the_items_id_does_not_count_as_the_item(
    fake: FakeConnect,
) -> None:
    fake.item("v1", "i1")
    fake.fail_after = {"DELETE": 500}
    moved = store(fake).move("ops", "db", "spare")
    assert "i1" not in fake.items["v1"]
    assert fake.items["v2"][moved.id]["title"] == "db"


def test_a_failed_check_after_a_failed_delete_keeps_the_copy(
    fake: FakeConnect,
) -> None:
    fake.fail = {"DELETE": 503}
    real = fake.handle

    def handle(method: str, path: str, auth: str, body: Json | None) -> object:
        # The check after the failed delete is the first GET of vault v1's items
        # that follows a DELETE.
        if (method, path) == ("GET", "/v1/vaults/v1/items") and any(
            m == "DELETE" for m, _ in fake.requests
        ):
            fake.requests.append((method, path))
            return 503
        return real(method, path, auth, body)

    fake.handle = handle  # type: ignore[method-assign]
    with pytest.raises(secrets.SecretsError) as caught:
        store(fake).move("ops", "db", "spare")
    assert str(caught.value) == (
        "the move couldn't tell whether it deleted item 'db' from vault 'ops',"
        " so it kept the copy in vault 'spare'"
    )
    assert fake.items["v1"]["i1"]["title"] == "db"
    assert [i["title"] for i in fake.items["v2"].values()] == ["db"]


def test_a_failed_cleanup_says_a_copy_may_remain(fake: FakeConnect) -> None:
    fake.fail_after = {"POST": 500}
    fake.fail = {"DELETE": 403}
    with pytest.raises(secrets.SecretsError, match="may remain in vault 'spare'"):
        store(fake).move("ops", "db", "spare")
    assert fake.items["v1"]["i1"]["title"] == "db"


def test_a_failed_cleanup_after_a_failed_delete_says_a_copy_may_remain(
    fake: FakeConnect,
) -> None:
    fake.fail = {"DELETE": 403}
    with pytest.raises(secrets.SecretsError, match="may remain in vault 'spare'"):
        store(fake).move("ops", "db", "spare")
    assert fake.items["v1"]["i1"]["title"] == "db"
    assert [i["title"] for i in fake.items["v2"].values()] == ["db"]


def test_move_refuses_an_item_with_files(fake: FakeConnect) -> None:
    fake.items["v1"]["i1"]["files"] = [{"id": "file1"}]
    with pytest.raises(secrets.SecretsError, match="has files"):
        store(fake).move("ops", "db", "spare")
    assert fake.items["v2"] == {}


def test_move_refuses_a_title_the_destination_has(fake: FakeConnect) -> None:
    fake.item("v2", "db", password="other")  # noqa: S106
    with pytest.raises(secrets.SecretsError, match="vault 'spare' already has an item"):
        store(fake).move("ops", "db", "spare")
    assert "i1" in fake.items["v1"]


def test_store_defaults_to_connect_with_the_keychain_token(
    fake: FakeConnect,
    config: Callable[[list[str]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config([fake.url])
    monkeypatch.setenv(secrets.TEST_TOKEN_ENV, fake.token)
    assert secrets.Store().get("ops", "db").title == "db"
    assert "Authorization" not in repr(secrets.connect())


@pytest.mark.parametrize(("returncode", "stdout"), [(0, ""), (1, "fake-token\n")])
def test_macos_rejects_empty_or_failed_keychain_reads(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    resolved_security: str,
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == resolved_security
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == (
        "no Connect token in this user's keychain; "
        "run `tbhbdev host bootstrap` for this host"
    )


def test_linux_replaces_invalid_utf8(linux_home: Path) -> None:
    path = token_file(linux_home)
    path.write_bytes(b"fake-\xff-token\n")
    assert secrets.token() == "fake-\ufffd-token"


@pytest.mark.parametrize("fail", ["directory", "file", "read", "none"])
def test_linux_token_descriptors_are_checked_and_closed(
    linux_home: Path, monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    path = token_file(linux_home)
    real_open, real_fstat, real_read = os.open, os.fstat, os.read
    opened: list[int] = []
    failed = False

    def open_checked(name: str | Path, flags: int, **kwargs: int) -> int:
        if not opened:
            assert name == path.parent
            assert flags == os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            assert kwargs == {}
        else:
            assert name == "connect-token"
            assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            assert kwargs == {"dir_fd": opened[0]}
            moved = linux_home / "moved"
            path.parent.rename(moved)
            path.parent.mkdir()
            token_file(linux_home, "replacement")
        fd = real_open(name, flags, **kwargs)
        opened.append(fd)
        return fd

    def fstat_checked(fd: int) -> os.stat_result:
        nonlocal failed
        if (
            fd in opened
            and not failed
            and fail == ("directory" if fd == opened[0] else "file")
        ):
            failed = True
            message = "stat failed"
            raise OSError(message)
        return real_fstat(fd)

    def read_checked(fd: int, count: int) -> bytes:
        assert count == secrets.MAX_TOKEN_FILE + 1
        if fail == "read":
            message = "read failed"
            raise OSError(message)
        return real_read(fd, count)

    monkeypatch.setattr(secrets.os, "open", open_checked)
    monkeypatch.setattr(secrets.os, "fstat", fstat_checked)
    monkeypatch.setattr(secrets.os, "read", read_checked)
    if fail == "none":
        assert secrets.token() == "fake-linux-token"
    else:
        with pytest.raises(secrets.SecretsError, match="can't be opened"):
            secrets.token()
    for fd in opened:
        with pytest.raises(OSError, match="Bad file descriptor"):
            real_fstat(fd)


@pytest.mark.parametrize(
    "size",
    [secrets.MAX_TOKEN_FILE - 1, secrets.MAX_TOKEN_FILE, secrets.MAX_TOKEN_FILE + 1],
)
def test_linux_token_size_limit(linux_home: Path, size: int) -> None:
    path = token_file(linux_home, "x" * size)
    if size > secrets.MAX_TOKEN_FILE:
        with pytest.raises(secrets.SecretsError) as caught:
            secrets.token()
        assert str(caught.value) == refused(
            path, f"exceeds {secrets.MAX_TOKEN_FILE} bytes"
        )
    else:
        assert secrets.token() == "x" * size
