"""Tests for tbhbdev.secrets, against fake Connect APIs on 127.0.0.1.

The keychain tests run stub security, secret-tool, dbus-run-session and
gnome-keyring-daemon executables with HOME in tmp_path (see conftest.py).
"""

from __future__ import annotations

import functools
import gc
import json
import shlex
import socket
import subprocess
import traceback
from typing import TYPE_CHECKING, override

import httpx
import pytest
from onepasswordconnectsdk.client import Client

from tbhbdev import secrets

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import FakeConnect, Installed, Stubs
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
    with pytest.raises(secrets.SecretsError, match="no Connect token"):
        secrets.token()
    # Not even security's own complaint about a missing keychain.
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("value", ["fake-token ", "fake-tokenX"])
def test_token_strips_only_the_newline_security_adds(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    mac_keychain(stubs, tmp_path / "home", value)
    assert secrets.token() == value


def test_token_on_linux_reads_the_gnome_keyring(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Linux")
    keyring = tmp_path / "home" / ".local" / "share" / "keyrings" / "stub.json"
    keyring.parent.mkdir(parents=True)
    key = f"service {secrets.SERVICE} account {secrets.ACCOUNT}"
    keyring.write_text(json.dumps({key: "fake-linux-token"}))
    assert secrets.token() == "fake-linux-token"
    assert stubs.argvs("secret-tool") == [
        ["lookup", "service", secrets.SERVICE, "account", secrets.ACCOUNT]
    ]
    assert [c["stdin"] for c in stubs.calls("gnome-keyring-daemon")] == [""]


def test_token_ignores_output_from_a_failed_lookup(
    stubs: Stubs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Linux")
    keyring = tmp_path / "home" / ".local" / "share" / "keyrings" / "stub.json"
    keyring.parent.mkdir(parents=True)
    key = f"service {secrets.SERVICE} account {secrets.ACCOUNT}"
    keyring.write_text(json.dumps({key: "fake-linux-token"}))
    stubs.set(secret_tool_rc=1)
    with pytest.raises(secrets.SecretsError) as caught:
        secrets.token()
    assert str(caught.value) == (
        "no Connect token in this user's keychain; "
        "run `tbhbdev host bootstrap` for this host"
    )


def test_token_on_linux_without_an_entry_fails(
    stubs: Stubs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Linux")
    with pytest.raises(secrets.SecretsError, match="no Connect token"):
        secrets.token()
    assert stubs.argvs("secret-tool") == [
        ["lookup", "service", secrets.SERVICE, "account", secrets.ACCOUNT]
    ]


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


def test_token_on_macos_runs_security_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Darwin")
    commands = fake_runs(monkeypatch)
    assert secrets.token() == "fake-token"
    assert [argv[:2] for argv in commands] == [
        ["security", "unlock-keychain"],
        ["security", "find-generic-password"],
    ]


def test_token_on_linux_runs_secret_tool_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secrets.platform, "system", lambda: "Linux")
    commands = fake_runs(monkeypatch)
    assert secrets.token() == "fake-token"
    lookup = ["secret-tool", "lookup", "service", secrets.SERVICE]
    lookup += ["account", secrets.ACCOUNT]
    assert len(commands) == 1
    assert commands[0][-1].endswith(f" && {shlex.join(lookup)}")


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


def test_a_failed_cleanup_says_a_copy_may_remain(fake: FakeConnect) -> None:
    fake.fail_after = {"POST": 500}
    fake.fail = {"DELETE": 403}
    with pytest.raises(secrets.SecretsError, match="may remain in vault 'spare'"):
        store(fake).move("ops", "db", "spare")
    assert fake.items["v1"]["i1"]["title"] == "db"


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
