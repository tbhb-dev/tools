"""Tests for `tbhbdev host`.

They run against stub op, tailscale, ssh, sudo, security, secret-tool,
dbus-run-session, gnome-keyring-daemon and swift executables, and a fake
Connect API on 127.0.0.1 (see conftest.py). The stub ssh runs the remote
command here with HOME under tmp_path, so each host's agent keychain is a file
in the test's scratch directory.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import httpx
import pytest
from onepasswordconnectsdk.client import Client

from tbhbdev import cli, secrets, userconfig
from tbhbdev import host as host_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import FakeConnect, Stubs

type Json = dict[str, object]

PROG = "tbhbdev host"
ACCOUNT = "example.1password.com"
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
SUFFIX = "example.ts.net"
ENDPOINTS = [f"https://nas-a.{SUFFIX}", f"https://nas-b.{SUFFIX}"]
TAILNET = {
    "MagicDNSSuffix": SUFFIX,
    "Self": {
        "HostName": "host-b",
        "DNSName": "host-b.example.ts.net.",
        "OS": "macOS",
        "Tags": ["tag:example-agents"],
    },
    "Peer": {
        "n1": {
            "HostName": "HOST-A",
            "DNSName": "host-a.example.ts.net.",
            "OS": "macOS",
            "Tags": ["tag:mac", "tag:example-agents"],
        },
        "n2": {
            "HostName": "host-c",
            "DNSName": "",
            "OS": "linux",
            "Tags": ["tag:example-agents"],
        },
        "n3": {"HostName": "nas-a", "OS": "linux", "Tags": ["tag:server"]},
        "n4": {"HostName": "phone", "OS": "iOS"},
    },
}
HOSTS = ("host-a", "host-b", "host-c")
REAL_CONNECT = secrets.Connect
# Stub ssh names a remote host's home after it; this machine's is "local".
HOME_KEY = {"host-a": "host-a", "host-b": "local", "host-c": "host-c"}


@dataclass
class World:
    stubs: Stubs
    fake: FakeConnect
    # The endpoints of each new token's check, which the fake serves.
    checked: list[list[str]] = field(default_factory=list)
    out: str = ""
    err: str = ""

    def account(self) -> Json:
        return cast("Json", self.stubs.state().get("op", {}))

    def rows(self, key: str) -> list[Json]:
        return cast("list[Json]", self.account().get(key, []))

    def tokens(self, state: str = "ACTIVE") -> list[Json]:
        return [t for t in self.rows("tokens") if t["state"] == state]

    def token_of(self, host: str) -> Json:
        mine = [t for t in self.tokens() if str(t["name"]).split(":")[1] == host]
        assert len(mine) == 1
        return mine[0]

    def installed(self, host: str) -> str | None:
        """The token in HOST's agent keychain, or None."""
        home = self.stubs.home(HOME_KEY[host])
        if host == "host-c":
            path = home / ".local" / "share" / "keyrings" / "stub.json"
            key = f"service {secrets.SERVICE} account {secrets.ACCOUNT}"
        else:
            path = home / "Library" / "Keychains" / f"{secrets.KEYCHAIN}-db"
            key = f"{secrets.SERVICE}/{secrets.ACCOUNT}"
        if not path.exists():
            return None
        data = cast("Json", json.loads(path.read_text()))
        items = cast("Json", data.get("items", data))
        value = items.get(key)
        return value if isinstance(value, str) else None

    def op_calls(self) -> list[str]:
        """Each op call's command, such as `connect token create`."""
        commands: list[str] = []
        for argv in self.stubs.argvs("op"):
            assert argv[-2:] == ["--account", ACCOUNT] or argv[-4:-2] == [
                "--account",
                ACCOUNT,
            ]
            commands.append(" ".join(argv[:3] if argv[0] == "connect" else argv[:2]))
        return commands

    def assert_no_token_leaks(self) -> None:
        """No token value in any argv, in the output, or in an error."""
        values = [str(t["token"]) for t in self.rows("tokens")]
        assert values
        for call in self.stubs.calls():
            text = json.dumps(call["argv"])
            assert not any(v in text for v in values), call
            if any(v in str(call.get("stdin", "")) for v in values):
                argv = cast("list[str]", call["argv"])
                assert (call["tool"], argv[0]) in {
                    ("security", "-i"),
                    ("secret-tool", "store"),
                }
        for stream in (self.out, self.err):
            assert not any(v in stream for v in values)


@pytest.fixture
def world(
    stubs: Stubs,
    connect: Callable[[], FakeConnect],
    config: Callable[[list[str] | None], None],
    monkeypatch: pytest.MonkeyPatch,
) -> World:
    fake = connect()
    for name in ("vault-all", "ops", "extra", *(f"vault-{h}" for h in HOSTS)):
        fake.vault(name)
    fake.tokens_file = stubs.dir / "connect-tokens.json"
    config(None)
    checked: list[list[str]] = []
    real = secrets.Connect

    def through_the_fake(value: str, urls: list[str]) -> secrets.Connect:
        checked.append(urls)
        return real(value, [fake.url])

    monkeypatch.setattr(host_tool.secrets, "Connect", through_the_fake)
    monkeypatch.setattr(host_tool, "now", lambda: T0)
    monkeypatch.setattr(host_tool.socket, "gethostname", lambda: "host-b.local")
    monkeypatch.setattr(host_tool.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host_tool, "SWIFT", str(stubs.bin / "swift"))
    stubs.set(tailscale=TAILNET)
    return World(stubs, fake, checked)


def run(world: World, capfd: pytest.CaptureFixture[str], *argv: str) -> int:
    code = host_tool.main(list(argv), PROG)
    captured = capfd.readouterr()
    world.out, world.err = captured.out, captured.err
    return code


def name_for(host: str, *vaults: str, when: datetime = T0) -> str:
    return host_tool.token_name(host, when, {"vault-all", f"vault-{host}", *vaults})


def test_the_command_is_registered() -> None:
    assert cli.TOOLS["host"] is host_tool.main
    assert "agent" not in cli.TOOLS


# list and host selection.


def test_list_shows_tagged_hosts_and_marks_this_one(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "list") == 0
    assert world.out == (
        "host-a\tmacOS\nhost-b\tmacOS\t(this machine)\nhost-c\tlinux\n"
    )


def test_list_finds_tailscale_in_the_app_bundle(
    world: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = world.stubs.dir / "Tailscale"
    shutil.move(world.stubs.bin / "tailscale", app)
    monkeypatch.setattr(host_tool, "TAILSCALE_APP", str(app))
    assert run(world, capfd, "list") == 0
    assert "host-a" in world.out


# macOS finds a stub tool whatever the case of its name, so these two check
# the names host runs.


@pytest.mark.usefixtures("world")
def test_tailnet_looks_tailscale_up_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    real = shutil.which

    def which(name: str) -> str | None:
        asked.append(name)
        return real(name)

    monkeypatch.setattr(host_tool.shutil, "which", which)
    assert host_tool.tailnet()["MagicDNSSuffix"] == SUFFIX
    assert asked == ["tailscale"]


def test_on_host_runs_sh_as_the_agent_through_sudo(stubs: Stubs) -> None:
    here = host_tool.Host("host-b", "macOS", local=True)
    done = host_tool.on_host(here, "echo hi")
    assert isinstance(done, subprocess.CompletedProcess)
    assert done.stdout == "hi\n"
    assert done.args == ["sudo", "-n", "-u", "agent", "-H", "sh", "-c", "echo hi"]
    assert stubs.argvs("sudo") == [["-n", "-u", "agent", "-H", "sh", "-c", "echo hi"]]


@pytest.mark.parametrize(
    ("state", "error"),
    [
        ({"tailscale_error": "boom"}, "tailscale status failed: tailscale: boom"),
        ({"tailscale": "not json"}, "tailscale status printed something other"),
        ({"tailscale": []}, "tailscale status printed something other than an object"),
    ],
)
def test_list_fails_on_a_tailscale_failure(
    world: World, capfd: pytest.CaptureFixture[str], state: Json, error: str
) -> None:
    world.stubs.set(**state)
    assert run(world, capfd, "list") == 1
    assert world.err.startswith(f"{PROG}: {error}")


def test_list_skips_malformed_nodes(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(tailscale={"Self": None, "Peer": None})
    assert run(world, capfd, "list") == 0
    assert world.out == ""


def test_a_hosts_name_is_the_first_label_of_its_dns_name(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    tag = ["tag:example-agents"]
    node = {"HostName": "laptop", "DNSName": "Host-B.example.ts.net.", "OS": "macOS"}
    other = {"HostName": "Host-D", "DNSName": "", "OS": "linux"}
    tailnet = {"Self": {**node, "Tags": tag}, "Peer": {"n1": {**other, "Tags": tag}}}
    world.stubs.set(tailscale=tailnet)
    assert run(world, capfd, "list") == 0
    assert world.out == "host-b\tmacOS\t(this machine)\nhost-d\tlinux\n"


def test_host_names_must_be_tagged(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "status", "--host", "nas-a", "--host", "x") == 1
    assert world.err == f"{PROG}: not tagged tag:example-agents: nas-a, x\n"


def edit_config(old: str, new: str) -> Path:
    """Replace OLD with NEW in the test's user config, and return its path."""
    path = userconfig.path()
    path.write_text(path.read_text().replace(old, new))
    return path


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (None, "{path} doesn't exist; create it and set [agent] host_tag\n"),
        (("[agent]", "[agent"), "cannot parse {path}: "),
        (('host_tag = "tag:example-agents"', ""), "{path} sets no [agent] host_tag\n"),
        (
            ('"tag:example-agents"', '""'),
            "{path}: [agent] host_tag must be a non-empty string\n",
        ),
        (('"tag:example-agents"', "1"), "{path}: [agent] host_tag must be a non-empty"),
    ],
)
def test_the_host_tag_comes_from_the_config(
    world: World,
    capfd: pytest.CaptureFixture[str],
    edit: tuple[str, str] | None,
    error: str,
) -> None:
    if edit is None:
        userconfig.path().unlink()
    else:
        edit_config(*edit)
    assert run(world, capfd, "list") == 1
    assert world.err.startswith(f"{PROG}: {error.format(path=userconfig.path())}")
    assert world.stubs.calls() == []


def test_another_host_tag_selects_other_hosts(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    text = config_file.read_text().replace("tag:example-agents", "tag:mac")
    config_file.write_text(text)
    assert run(world, capfd, "list") == 0
    assert world.out == "host-a\tmacOS\n"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            ('server_hosts = ["nas-a", "nas-b"]', ""),
            "{path} sets no [connect] server_hosts",
        ),
        (
            ('["nas-a", "nas-b"]', "[]"),
            "{path}: [connect] server_hosts must be a non-empty list of strings",
        ),
    ],
)
def test_bootstrap_needs_the_server_hosts_before_creating_a_server(
    world: World,
    capfd: pytest.CaptureFixture[str],
    edit: tuple[str, str],
    message: str,
) -> None:
    path = edit_config(*edit)
    assert run(world, capfd, "bootstrap", "--all") == 1
    assert world.err == f"{PROG}: {message.format(path=path)}\n"
    assert world.op_calls() == []


@pytest.mark.parametrize(
    ("edit", "argv", "error"),
    [
        (
            ('server = "example-connect"', 'server = "other-connect"'),
            ["grant", "--all", "ops"],
            "there is no Connect server other-connect; run bootstrap first",
        ),
        (
            ('account = "example.1password.com"', ""),
            ["grant", "--all", "ops"],
            "{path} sets no [op] account",
        ),
        (
            ('host_prefix = "vault-"', ""),
            ["revoke", "--all", "ops"],
            "{path} sets no [vaults] host_prefix",
        ),
        (
            ('all = "vault-all"', 'all = "shared"'),
            ["revoke", "--host", "host-a", "shared"],
            "host-a always keeps shared",
        ),
        (
            ('host_prefix = "vault-"', 'host_prefix = "own-"'),
            ["revoke", "--host", "host-a", "own-host-a"],
            "host-a always keeps own-host-a",
        ),
    ],
)
def test_messages_name_the_configured_values(
    world: World,
    capfd: pytest.CaptureFixture[str],
    edit: tuple[str, str],
    argv: list[str],
    error: str,
) -> None:
    path = edit_config(*edit)
    assert run(world, capfd, *argv) == 1
    assert world.err == f"{PROG}: {error.format(path=path)}\n"


def test_op_names_the_configured_account(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    text = config_file.read_text().replace("example.1password.com", "other.example")
    config_file.write_text(text)
    assert run(world, capfd, "grant", "--all", "ops") == 1
    assert world.stubs.argvs("op") == [
        ["connect", "server", "list", "--format", "json", "--account", "other.example"]
    ]


def test_a_token_prefix_from_the_config_names_the_tokens(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    first = world.token_of("host-a")
    text = config_file.read_text().replace('"example-host"', '"other-host"')
    config_file.write_text(text)
    assert run(world, capfd, "revoke", "--host", "host-a", "ops") == 0
    assert "host-a: no token; creating a new token" in world.out
    names = [str(t["name"]) for t in world.tokens()]
    assert names == [first["name"], name_for("host-a")]
    assert names[1].startswith("other-host:host-a:")


def test_this_machine_must_be_tagged_when_no_host_is_named(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    tailnet = dict(TAILNET)
    tailnet["Self"] = {"HostName": "host-b", "OS": "macOS"}
    world.stubs.set(tailscale=tailnet)
    assert run(world, capfd, "status") == 1
    assert world.err == (
        f"{PROG}: this machine isn't tagged tag:example-agents; name --host or --all\n"
    )


# bootstrap.


def test_bootstrap_from_nothing_sets_up_every_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all", "ops") == 0
    lines = world.out.splitlines()
    credentials = Path(lines[1].removeprefix("its credentials file is "))
    assert lines[:3] == [
        "created the Connect server example-connect",
        f"its credentials file is {credentials}",
        "install it on nas-a and nas-b, then delete it",
    ]
    assert credentials.name == host_tool.CREDENTIALS
    assert credentials.exists()
    assert world.checked == [ENDPOINTS] * len(HOSTS)
    assert credentials.parent.stat().st_mode & 0o777 == 0o700
    assert "stub" not in world.out
    vaults = ["ops", "vault-all", "vault-host-a", "vault-host-b", "vault-host-c"]
    assert [v["name"] for v in world.rows("vaults")] == vaults
    assert world.account()["grants"] == vaults
    for host in HOSTS:
        token = world.token_of(host)
        assert token["name"] == name_for(host, "ops")
        assert token["expires_in"] == "90d"
        assert world.installed(host) == token["token"]
        assert f"{host}: installed the token {token['name']}" in lines
    keychain = json.loads(
        (
            world.stubs.home("host-a")
            / "Library"
            / "Keychains"
            / f"{secrets.KEYCHAIN}-db"
        ).read_text()
    )
    assert (keychain["password"], keychain["locked"], keychain["settings"]) == (
        "",
        False,
        [],
    )
    world.assert_no_token_leaks()


def test_bootstrap_again_has_nothing_to_do(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all", "ops") == 0
    before = len(world.op_calls())
    assert run(world, capfd, "bootstrap", "--all", "ops") == 0
    changes = [c for c in world.op_calls()[before:] if not c.endswith("list")]
    assert changes == []
    for host in HOSTS:
        assert f"{host}: the token {name_for(host, 'ops')} is current" in world.out
    world.assert_no_token_leaks()


def test_bootstrap_keeps_vaults_a_host_already_has(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "ops") == 0
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert "is current" in world.out
    assert world.token_of("host-a")["name"] == name_for("host-a", "ops")


def test_wrong_vaults_get_a_new_token_and_the_old_one_goes(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    old = world.token_of("host-a")
    assert run(world, capfd, "bootstrap", "--host", "host-a", "ops") == 0
    assert world.out.startswith(
        f"created the vault ops\n"
        f"granted the Connect server the vault ops\n"
        f"host-a: config.toml ok\n"
        f"host-a: the token {old['name']} has other vaults; creating a new token\n"
    )
    new = world.token_of("host-a")
    assert new["name"] == name_for("host-a", "ops")
    assert world.installed("host-a") == new["token"]
    assert [t["id"] for t in world.tokens("REVOKED")] == [old["id"]]
    world.assert_no_token_leaks()


@pytest.mark.parametrize(("days", "renewed"), [(75, False), (76, True), (91, True)])
def test_a_token_near_expiry_is_renewed(
    world: World,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    days: int,
    renewed: bool,
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-c") == 0
    old = world.token_of("host-c")
    later = T0 + timedelta(days=days)
    monkeypatch.setattr(host_tool, "now", lambda: later)
    assert run(world, capfd, "bootstrap", "--host", "host-c") == 0
    new = world.token_of("host-c")
    assert (new["id"] != old["id"]) is renewed
    if renewed:
        expiry = f"{T0 + host_tool.LIFETIME:%Y-%m-%d}"
        assert f"the token {old['name']} expires {expiry}" in world.out
        assert new["name"] == name_for("host-c", when=later)
        assert world.installed("host-c") == new["token"]
    world.assert_no_token_leaks()


def test_a_missing_token_is_created(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-b") == 0
    state = world.stubs.state()
    for token in cast("list[Json]", cast("Json", state["op"])["tokens"]):
        token["state"] = "REVOKED"
    world.stubs.set(**state)
    assert run(world, capfd, "bootstrap", "--host", "host-b") == 0
    assert "host-b: no token; creating a new token" in world.out
    assert world.installed("host-b") == world.token_of("host-b")["token"]


def test_a_missing_keychain_entry_gets_a_new_token(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    old = world.token_of("host-a")
    path = world.stubs.home("host-a") / "Library" / "Keychains"
    path = path / f"{secrets.KEYCHAIN}-db"
    data = json.loads(path.read_text())
    data["items"] = {}
    path.write_text(json.dumps(data))
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert "host-a: the keychain has no token; creating a new token" in world.out
    assert world.token_of("host-a")["id"] != old["id"]
    assert world.installed("host-a") == world.token_of("host-a")["token"]


def test_stray_tokens_beside_a_current_one_are_deleted(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    state = world.stubs.state()
    tokens = cast("list[Json]", cast("Json", state["op"])["tokens"])
    older = name_for("host-a", when=T0 - timedelta(days=1))
    stray = {**tokens[0], "id": "tok-stray", "name": older, "token": "fake-stray"}
    tokens.append(stray)
    world.stubs.set(**state)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert f"deleted the token {older}" in world.out
    assert [t["id"] for t in world.tokens("REVOKED")] == ["tok-stray"]


@pytest.mark.parametrize(
    "entry",
    [
        "nas/b",
        "nas b",
        "https://nas-b",
        "nas_b",
        "nas.b",
        "-nas",
        "nas-",
        "b" * 64,
        "nas-b\n",
        "nás",
    ],
)
def test_a_server_host_must_be_a_dns_label(
    world: World, capfd: pytest.CaptureFixture[str], entry: str
) -> None:
    path = edit_config('"nas-b"', json.dumps(entry))
    assert run(world, capfd, "bootstrap", "--all") == 1
    assert world.err == (
        f"{PROG}: {path}: entry 2 of [connect] server_hosts isn't a DNS label\n"
    )
    assert world.op_calls() == []
    assert world.stubs.calls("sudo") == []
    assert_suffix_unseen(world)


def test_an_empty_server_host_is_refused(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    path = edit_config('"nas-b"', '""')
    assert run(world, capfd, "bootstrap", "--all") == 1
    assert world.err == (
        f"{PROG}: {path}: [connect] server_hosts must be a non-empty list of strings\n"
    )
    assert world.op_calls() == []


def test_any_dns_label_is_a_server_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    labels = ["NAS-A", "b", "0-9", "c" * 63]
    edit_config('["nas-a", "nas-b"]', json.dumps(labels))
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert world.checked == [[f"https://{h}.{SUFFIX}" for h in labels]]


def test_a_failed_check_keeps_the_urls_out_of_the_error(
    world: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing(Client):
        @override
        def build_request(
            self, method: str, path: str, body: object = None
        ) -> httpx.Response:
            del method, path, body
            message = f"cannot decode the answer from {self.url}"
            raise httpx.DecodingError(message)

    # The new token's check holds the real endpoints, and every request fails
    # without leaving this machine.
    monkeypatch.setattr(secrets, "Connect", REAL_CONNECT)
    monkeypatch.setattr(secrets, "Client", Failing)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err == (
        f"{PROG}: the new token can't list its vaults: "
        "Connect can't send GET /v1/vaults: DecodingError\n"
    )
    assert_suffix_unseen(world)
    assert world.tokens() == []


# The agent's config.


def copy_of(world: World, host: str) -> Path:
    """HOST's agent copy of the user config."""
    return world.stubs.home(HOME_KEY[host]) / ".config" / "tbhbdev" / "config.toml"


def agent_copy_text() -> str:
    """The copy bootstrap installs from the example config."""
    return (
        '[op]\naccount = "example.1password.com"\n\n'
        '[connect]\nserver = "example-connect"\n'
        'server_hosts = ["nas-a", "nas-b"]\n'
        'token_prefix = "example-host"\n'
        f'endpoints = ["https://nas-a.{SUFFIX}", "https://nas-b.{SUFFIX}"]\n\n'
        '[agent]\nhost_tag = "tag:example-agents"\n\n'
        '[vaults]\nall = "vault-all"\nhost_prefix = "vault-"\n'
    )


def assert_suffix_unseen(world: World) -> None:
    """The tailnet's suffix is in no argv, output or error."""
    for call in world.stubs.calls():
        assert SUFFIX not in json.dumps(call["argv"])
    assert SUFFIX not in world.out + world.err


def test_bootstrap_installs_the_config_on_each_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all") == 0
    text = agent_copy_text()
    for host in HOSTS:
        assert f"{host}: config.toml changed" in world.out.splitlines()
        copy = copy_of(world, host)
        assert copy.read_text() == text
        assert copy.stat().st_mode & 0o777 == 0o600
        assert copy.parent.stat().st_mode & 0o777 == 0o700
        assert [p.name for p in copy.parent.iterdir()] == ["config.toml"]
    for call in world.stubs.calls():
        assert "[connect]" not in json.dumps(call["argv"])
    assert "[connect]" not in world.out + world.err
    assert_suffix_unseen(world)
    assert run(world, capfd, "bootstrap", "--all") == 0
    for host in HOSTS:
        assert f"{host}: config.toml ok" in world.out.splitlines()


def test_the_copy_replaces_the_operators_endpoints(
    world: World,
    config: Callable[[list[str] | None], None],
    capfd: pytest.CaptureFixture[str],
) -> None:
    config(["https://elsewhere.example"])
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert copy_of(world, "host-a").read_text() == (
        '[op]\naccount = "example.1password.com"\n\n'
        '[connect]\nserver = "example-connect"\n'
        f'endpoints = ["https://nas-a.{SUFFIX}", "https://nas-b.{SUFFIX}"]\n'
        'server_hosts = ["nas-a", "nas-b"]\n'
        'token_prefix = "example-host"\n\n'
        '[agent]\nhost_tag = "tag:example-agents"\n\n'
        '[vaults]\nall = "vault-all"\nhost_prefix = "vault-"\n'
    )


def test_the_endpoints_follow_the_server_hosts(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    edit_config('["nas-a", "nas-b"]', '["nas-b", "nas-c", "nas-a"]')
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    copy = tomllib.loads(copy_of(world, "host-a").read_text())
    urls = [f"https://{h}.{SUFFIX}" for h in ("nas-b", "nas-c", "nas-a")]
    assert cast("Json", copy["connect"])["endpoints"] == urls
    assert world.checked == [urls]


def test_bootstrap_copies_nothing_it_cant_render(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text(config_file.read_text() + "[extra]\nport = 1\n")
    assert run(world, capfd, "bootstrap", "--all") == 1
    assert world.err == (
        f"{PROG}: {config_file}: [extra] port must be a string or a list of strings\n"
    )
    assert world.stubs.calls("op") == []
    for host in HOSTS:
        assert not copy_of(world, host).exists()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("missing", "{app} not found"),
        ("failing", "tailscale status failed: tailscale: boom"),
        ("empty", "tailscale status gives no MagicDNSSuffix, so the Connect"),
        ("absent", "tailscale status gives no MagicDNSSuffix, so the Connect"),
        ("number", "tailscale status gives no MagicDNSSuffix, so the Connect"),
    ],
)
@pytest.mark.parametrize("command", ["bootstrap", "status", "grant", "revoke"])
def test_no_config_is_written_without_the_tailnet_suffix(
    world: World,
    capfd: pytest.CaptureFixture[str],
    change: str,
    error: str,
    command: str,
) -> None:
    tailnet = json.loads(json.dumps(TAILNET))
    if change == "missing":
        (world.stubs.bin / "tailscale").unlink()
    elif change == "failing":
        world.stubs.set(tailscale_error="boom")
    elif change == "absent":
        del tailnet["MagicDNSSuffix"]
    else:
        tailnet["MagicDNSSuffix"] = "" if change == "empty" else 7
    if change not in {"missing", "failing"}:
        world.stubs.set(tailscale=tailnet)
    vaults: list[str] = [] if command in {"bootstrap", "status"} else ["ops"]
    assert run(world, capfd, command, "--all", *vaults) == 1
    assert world.err.startswith(f"{PROG}: {error.format(app=host_tool.TAILSCALE_APP)}")
    assert world.stubs.calls("op") == []
    assert world.stubs.calls("sudo") == []
    for host in HOSTS:
        assert not copy_of(world, host).exists()


def test_bootstrap_replaces_a_copy_that_differs(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    copy = copy_of(world, "host-a")
    copy.parent.mkdir(parents=True, mode=0o755)
    copy.write_text(config_file.read_text())
    copy.chmod(0o644)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert "host-a: config.toml changed\n" in world.out
    assert copy.read_text() == agent_copy_text()
    assert copy.stat().st_mode & 0o777 == 0o600
    assert copy.parent.stat().st_mode & 0o777 == 0o700
    copy.chmod(0o644)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert "host-a: config.toml ok\n" in world.out
    assert copy.stat().st_mode & 0o777 == 0o600


def test_bootstrap_fails_when_the_config_cant_be_installed(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    blocked = copy_of(world, "host-a").parent
    blocked.parent.mkdir(parents=True)
    blocked.write_text("")
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert (
        world.err
        == f"{PROG}: host-a: cannot install the agent's config.toml (exit 1)\n"
    )
    assert world.tokens() == []


def test_status_says_whether_each_copy_matches(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "--host", "host-c") == 0
    copy_of(world, "host-c").write_text(userconfig.path().read_text())
    assert run(world, capfd, "status", "--all") == 0
    lines = world.out.splitlines()
    assert [line.rsplit("; ", 1)[1] for line in lines] == [
        "config.toml matches",
        "config.toml missing",
        "config.toml differs",
    ]
    assert "[op]" not in world.out
    assert_suffix_unseen(world)


def test_status_fails_on_an_unexpected_config_check(
    world: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    check = ("echo nope", frozenset({"matches"}))
    monkeypatch.setitem(host_tool.CONFIG_STEPS, "check", check)
    assert run(world, capfd, "status") == 1
    assert (
        world.err == f"{PROG}: host-b: cannot check the agent's config.toml (exit 0)\n"
    )


# Failures while replacing a token.


def test_a_failed_install_deletes_the_new_token_and_keeps_the_old(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    old = world.token_of("host-a")
    world.stubs.set(security_i_broken=True)
    assert run(world, capfd, "bootstrap", "--host", "host-a", "ops") == 1
    assert world.err == f"{PROG}: host-a: installing the token failed (exit 44)\n"
    assert [t["id"] for t in world.tokens()] == [old["id"]]
    world.assert_no_token_leaks()


@pytest.mark.parametrize(
    ("break_connect", "error"),
    [
        (
            "down",
            (
                "the new token can't list its vaults: "
                "no Connect endpoint answered GET /v1/vaults"
            ),
        ),
        (
            "vault",
            "the new token lists ['vault-all'], not ['vault-all', 'vault-host-a']",
        ),
    ],
)
def test_a_failed_check_deletes_the_new_token(
    world: World, capfd: pytest.CaptureFixture[str], break_connect: str, error: str
) -> None:
    if break_connect == "down":
        world.fake.status = 503
    else:
        world.fake.vaults = {
            k: v for k, v in world.fake.vaults.items() if v == "vault-all"
        }
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err == f"{PROG}: {error}\n"
    assert world.tokens() == []
    world.assert_no_token_leaks()


@pytest.mark.parametrize(
    ("state", "error"),
    [
        (
            {"op_fail": {"connect token create": "quota"}},
            "failed: [ERROR] quota",
        ),
        (
            {"op_token_output": "not a token!"},
            "op connect token create printed no token for example-host:host-a:",
        ),
        ({"ssh_down": ["host-a"]}, "host-a: cannot set up the agent keychain"),
        ({"op_raw": {"vault list": "oops"}}, "op vault list printed something other"),
        (
            {"op_raw": {"connect server list": "[]"}},
            "there is no Connect server example-connect; run bootstrap first",
        ),
        (
            {"op_raw": {"connect server list": "null"}},
            "there is no Connect server example-connect; run bootstrap first",
        ),
    ],
)
def test_bootstrap_failures(
    world: World, capfd: pytest.CaptureFixture[str], state: Json, error: str
) -> None:
    world.stubs.set(**state)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert error in world.err
    assert world.tokens() == []


def test_a_host_with_another_os_is_refused(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    tailnet = json.loads(json.dumps(TAILNET))
    tailnet["Peer"]["n4"]["Tags"] = ["tag:example-agents"]
    world.stubs.set(tailscale=tailnet)
    assert run(world, capfd, "bootstrap", "--host", "phone") == 1
    assert world.err == f"{PROG}: phone: unsupported OS 'iOS'\n"


def test_op_missing_from_path(world: World, capfd: pytest.CaptureFixture[str]) -> None:
    (world.stubs.bin / "op").unlink()
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err == f"{PROG}: op not found\n"


# grant and revoke.


def test_grant_needs_a_server(world: World, capfd: pytest.CaptureFixture[str]) -> None:
    assert run(world, capfd, "grant", "--host", "host-a", "ops") == 1
    assert world.err == (
        f"{PROG}: there is no Connect server example-connect; run bootstrap first\n"
    )


def test_grant_adds_a_vault_to_one_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all") == 0
    assert run(world, capfd, "grant", "--host", "host-a", "extra") == 1
    assert world.err == f"{PROG}: no such vault: extra; run bootstrap to create it\n"
    state = world.stubs.state()
    vaults = cast("list[Json]", cast("Json", state["op"])["vaults"])
    vaults.append({"id": "vlt-extra", "name": "extra"})
    world.stubs.set(**state)
    others = {h: world.token_of(h)["id"] for h in ("host-b", "host-c")}
    assert run(world, capfd, "grant", "--host", "host-a", "extra") == 0
    assert "granted the Connect server the vault extra" in world.out
    assert world.token_of("host-a")["name"] == name_for("host-a", "extra")
    assert {h: world.token_of(h)["id"] for h in others} == others
    assert world.checked == [ENDPOINTS] * (len(HOSTS) + 1)
    world.assert_no_token_leaks()


def test_grant_all_runs_exactly_these_op_commands(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all") == 0
    state = world.stubs.state()
    vaults = cast("list[Json]", cast("Json", state["op"])["vaults"])
    vaults.append({"id": "vlt-extra", "name": "extra"})
    world.stubs.set(**state)
    (world.stubs.dir / "calls.jsonl").unlink()
    assert run(world, capfd, "grant", "--all", "extra") == 0
    server = ["--server", "srv-1"]
    listed = ["--format", "json", "--account", ACCOUNT]
    tokens = ["connect", "token", "list", *server, *listed]
    expected = [
        ["connect", "server", "list", *listed],
        tokens,
        ["vault", "list", *listed],
        [
            "connect",
            "vault",
            "grant",
            *server,
            "--vault",
            "extra",
            "--account",
            ACCOUNT,
        ],
    ]
    for number, host in enumerate(HOSTS, start=1):
        expected += [
            [
                "connect",
                "token",
                "create",
                name_for(host, "extra"),
                *server,
                *("--vault", "extra", "--vault", "vault-all"),
                *("--vault", f"vault-{host}", "--expires-in", "90d"),
                *("--account", ACCOUNT),
            ],
            ["connect", "token", "delete", f"tok-{number}", *server],
            tokens,
        ]
        expected[-2] += ["--account", ACCOUNT]
    assert world.stubs.argvs("op") == expected
    assert world.out == "granted the Connect server the vault extra\n" + "".join(
        f"{host}: the token {name_for(host)} has other vaults; creating a new token\n"
        f"deleted the token {name_for(host)}\n"
        f"{host}: installed the token {name_for(host, 'extra')}\n"
        for host in HOSTS
    )
    world.assert_no_token_leaks()


def test_grant_names_every_missing_vault(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert run(world, capfd, "grant", "--host", "host-a", "x-two", "x-one") == 1
    assert world.err == (
        f"{PROG}: no such vault: x-one, x-two; run bootstrap to create it\n"
    )


@pytest.mark.parametrize("vault", ["", "a,b"])
def test_vault_names_are_checked(
    world: World, capfd: pytest.CaptureFixture[str], vault: str
) -> None:
    assert run(world, capfd, "grant", "--all", vault) == 1
    assert "a vault name can't be empty or hold a comma" in world.err
    assert world.stubs.calls() == []


@pytest.mark.parametrize("vault", ["vault-all", "vault-host-a"])
def test_revoke_refuses_a_hosts_own_vaults(
    world: World, capfd: pytest.CaptureFixture[str], vault: str
) -> None:
    assert run(world, capfd, "revoke", "--host", "host-a", vault) == 1
    assert world.err == f"{PROG}: host-a always keeps {vault}\n"
    assert world.stubs.calls("op") == []


def test_revoke_names_every_vault_a_host_keeps(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    argv = ["revoke", "--host", "host-a", "vault-host-a", "ops", "vault-all"]
    assert run(world, capfd, *argv) == 1
    assert world.err == f"{PROG}: host-a always keeps vault-all, vault-host-a\n"
    assert world.stubs.calls("op") == []


def test_revoke_takes_from_the_server_only_the_vaults_it_names(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "ops") == 0
    state = world.stubs.state()
    account = cast("Json", state["op"])
    cast("list[str]", account["grants"]).append("extra")
    tokens = cast("list[Json]", account["tokens"])
    older = name_for("host-a", "extra", when=T0 - timedelta(days=1))
    tokens.append({**tokens[0], "id": "tok-stray", "name": older, "token": "fake-x"})
    world.stubs.set(**state)
    assert run(world, capfd, "revoke", "--host", "host-a", "ops") == 0
    assert world.out.endswith(
        f"host-a: installed the token {name_for('host-a')}\n"
        "revoked the Connect server's access to the vault ops\n"
    )
    assert world.account()["grants"] == ["vault-all", "vault-host-a", "extra"]


def test_revoke_keeps_server_access_while_another_token_uses_it(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all", "ops") == 0
    assert run(world, capfd, "revoke", "--host", "host-a", "ops") == 0
    assert world.token_of("host-a")["name"] == name_for("host-a")
    assert world.token_of("host-b")["name"] == name_for("host-b", "ops")
    assert "ops" in cast("list[str]", world.account()["grants"])
    assert "connect vault revoke" not in world.op_calls()
    world.assert_no_token_leaks()


def test_revoke_everywhere_takes_the_vault_from_the_server(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all", "ops") == 0
    assert run(world, capfd, "revoke", "--all", "ops") == 0
    assert "revoked the Connect server's access to the vault ops" in world.out
    assert "ops" not in cast("list[str]", world.account()["grants"])
    for host in HOSTS:
        assert world.installed(host) == world.token_of(host)["token"]
    assert world.checked == [ENDPOINTS] * 2 * len(HOSTS)
    world.assert_no_token_leaks()


def test_revoke_leaves_the_server_alone_beside_other_tokens(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "ops") == 0
    state = world.stubs.state()
    tokens = cast("list[Json]", cast("Json", state["op"])["tokens"])
    for name in ("ci", "example-host:x:not-a-time:ops"):
        tokens.append({**tokens[0], "id": f"tok-{name}", "name": name})
    world.stubs.set(**state)
    assert run(world, capfd, "revoke", "--host", "host-a", "ops") == 0
    assert world.out.endswith(
        "leaving the server's access to ops alone: "
        "2 token(s) on it aren't agent host tokens\n"
    )
    assert "ops" in cast("list[str]", world.account()["grants"])


# These two call host's functions directly, without bootstrap: on macOS, a
# Connect check in a process forked from a threaded one, as mutmut runs tests,
# can crash in the system proxy lookup.


@pytest.mark.usefixtures("world")
def test_a_new_server_gets_a_credentials_directory_named_after_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(host_tool.tempfile, "tempdir", str(tmp_path))
    assert host_tool.ensure_server() == "srv-1"
    line = capfd.readouterr().out.splitlines()[1]
    credentials = Path(line.removeprefix("its credentials file is "))
    assert credentials.parent.parent == tmp_path
    assert credentials.parent.name.startswith("example-connect-")


def test_an_account_counts_its_live_foreign_tokens(
    world: World,
) -> None:
    names = [name_for("host-a", "ops"), "ci", "example-host:x:not-a-time:ops", "old"]
    tokens: list[Json] = [
        {
            "id": f"tok-{i}",
            "name": name,
            "state": "REVOKED" if name == "old" else "ACTIVE",
            "integrationId": "srv-1",
            "token": f"fake-connect-token-{i}",
            "vaults": [],
        }
        for i, name in enumerate(names)
    ]
    servers = [{"id": "srv-1", "name": "example-connect"}]
    world.stubs.set(
        op={"servers": servers, "vaults": [], "grants": [], "tokens": tokens}
    )
    account = host_tool.Account.load("srv-1")
    assert [t.host for t in account.tokens] == ["host-a"]
    assert account.foreign == 2


def test_revoke_names_every_vault_it_leaves_alone(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "ops", "extra") == 0
    state = world.stubs.state()
    tokens = cast("list[Json]", cast("Json", state["op"])["tokens"])
    tokens.append({**tokens[0], "id": "tok-ci", "name": "ci"})
    world.stubs.set(**state)
    assert run(world, capfd, "revoke", "--host", "host-a", "ops", "extra") == 0
    assert world.out.endswith(
        "leaving the server's access to extra, ops alone: "
        "1 token(s) on it aren't agent host tokens\n"
    )


# status.


def test_status_without_a_server(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "status") == 0
    assert world.out == (
        "host-b: no token; keychain entry absent; config.toml missing\n"
    )


def test_status_shows_each_hosts_token_without_its_value(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "--host", "host-c") == 0
    assert run(world, capfd, "status", "--all") == 0
    assert world.out == (
        f"host-a: token {name_for('host-a')}; vaults vault-all, vault-host-a; "
        "expires 2027-02-02T12:00:00Z; keychain entry present; config.toml matches\n"
        "host-b: no token; keychain entry absent; config.toml missing\n"
        f"host-c: token {name_for('host-c')}; vaults vault-all, vault-host-c; "
        "expires 2027-02-02T12:00:00Z; keychain entry present; config.toml matches\n"
    )
    world.assert_no_token_leaks()


def test_status_fails_when_a_host_is_unreachable(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(ssh_down=["host-a"])
    assert run(world, capfd, "status", "--host", "host-a") == 1
    assert world.err.startswith(
        f"{PROG}: host-a: cannot check the agent keychain: ssh: connect"
    )


@pytest.mark.parametrize("script", ["echo maybe", "echo present; exit 3"])
def test_status_fails_on_an_unexpected_keychain_check(
    world: World,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    script: str,
) -> None:
    monkeypatch.setitem(host_tool.SCRIPTS["macOS"], "present", script)
    assert run(world, capfd, "status") == 1
    assert world.err == f"{PROG}: host-b: cannot check the agent keychain: \n"


def test_linux_scripts_run_in_a_private_keyring_session() -> None:
    assert host_tool._linux("echo hi") == (  # noqa: SLF001
        'dbus-run-session -- sh -c \'printf "" | gnome-keyring-daemon --unlock '
        "--components=secrets >/dev/null && echo hi'"
    )


# exec.


def test_exec_runs_here_as_the_agent_by_default(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "exec", "--", "sh", "-c", 'echo "$HOME"; exit 7') == 7
    assert world.out == f"{world.stubs.home()}\n"
    assert world.stubs.argvs("sudo") == [
        ["-n", "-u", "agent", "-H", "sh", "-c", 'echo "$HOME"; exit 7']
    ]
    assert world.stubs.calls("ssh") == []


@pytest.mark.parametrize("host", ["host-b", "HOST-B"])
def test_exec_on_this_machine_by_name_runs_here(
    world: World, capfd: pytest.CaptureFixture[str], host: str
) -> None:
    assert run(world, capfd, "exec", "--host", host, "--", "true") == 0
    assert world.stubs.calls("ssh") == []


def test_exec_on_another_host_goes_over_ssh(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(world, capfd, "exec", "--host", "host-a", "--", "sh", "-c", "exit 3") == 3
    )
    assert world.stubs.argvs("ssh") == [
        ["-o", "BatchMode=yes", "host-a", "sudo -n -u agent -H sh -c 'exit 3'"]
    ]


@pytest.mark.parametrize("host", [None, "host-a"])
def test_exec_operator_runs_as_the_operator_after_touch_id(
    world: World, capfd: pytest.CaptureFixture[str], host: str | None
) -> None:
    where: list[str] = [] if host is None else ["--host", host]
    assert run(world, capfd, "exec", *where, "--operator", "--", "echo", "hi") == 0
    assert world.out == "hi\n"
    assert world.stubs.calls("sudo") == []
    swift = world.stubs.calls("swift")
    assert [(c["argv"], c["stdin"]) for c in swift] == [(["-"], host_tool.TOUCH_ID)]
    if host is not None:
        assert world.stubs.argvs("ssh") == [
            ["-o", "BatchMode=yes", "host-a", "echo hi"]
        ]


@pytest.mark.parametrize(
    ("swift_rc", "error"),
    [
        (1, "the Touch ID check failed"),
        (2, "--operator needs Touch ID, which isn't available here"),
    ],
)
def test_exec_operator_refuses_without_touch_id(
    world: World, capfd: pytest.CaptureFixture[str], swift_rc: int, error: str
) -> None:
    world.stubs.set(swift_rc=swift_rc)
    assert run(world, capfd, "exec", "--operator", "--", "touch", "ran") == 1
    assert world.err == f"{PROG}: {error}\n"
    assert world.stubs.calls("sudo") == []
    assert not Path("ran").exists()


def test_exec_operator_refuses_off_macos(
    world: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_tool.platform, "system", lambda: "Linux")
    assert run(world, capfd, "exec", "--operator", "--", "true") == 1
    assert world.err == f"{PROG}: --operator needs macOS with Touch ID\n"
    assert world.stubs.calls() == []


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["exec"], "exec needs a command after --"),
        (["exec", "--"], "exec needs a command after --"),
        (
            ["exec", "--host=-oProxyCommand=x", "--", "true"],
            "not a host name: '-oProxyCommand=x'",
        ),
    ],
)
def test_exec_usage_errors(
    world: World, capfd: pytest.CaptureFixture[str], argv: list[str], error: str
) -> None:
    assert run(world, capfd, *argv) == 1
    assert world.err == f"{PROG}: {error}\n"
    assert world.stubs.calls() == []


@pytest.mark.parametrize(("argv", "code"), [([], 2), (["--help"], 0), (["nope"], 2)])
def test_usage(argv: list[str], code: int) -> None:
    assert host_tool.main(argv, PROG) == code


def test_help_says_status_shows_configs(capsys: pytest.CaptureFixture[str]) -> None:
    assert host_tool.main(["--help"], PROG) == 0
    help_line = re.compile(r"^ +status +show hosts' tokens and configs$", re.MULTILINE)
    assert help_line.search(capsys.readouterr().out)


COMMANDS = "{list,bootstrap,grant,revoke,status,exec}"
TARGETS = """\
options:
  -h, --help   show this help message and exit
  --host HOST  an agent host (repeatable)
  --all        every agent host
"""
HELP = {
    "": f"""\
usage: {PROG} [-h] {COMMANDS} ...

Manage agent hosts.

positional arguments:
  {COMMANDS}
    list                list the agent hosts
    bootstrap           set up hosts, idempotently
    grant               add vaults to hosts' tokens
    revoke              take vaults from hosts' tokens
    status              show hosts' tokens and configs
    exec                run a command on a host as the agent user

options:
  -h, --help            show this help message and exit
""",
    "list": f"""\
usage: {PROG} list [-h]

options:
  -h, --help  show this help message and exit
""",
    "bootstrap": f"""\
usage: {PROG} bootstrap [-h] [--host HOST | --all] [VAULT ...]

positional arguments:
  VAULT

{TARGETS}""",
    "grant": f"""\
usage: {PROG} grant [-h] [--host HOST | --all] VAULT [VAULT ...]

positional arguments:
  VAULT

{TARGETS}""",
    "revoke": f"""\
usage: {PROG} revoke [-h] [--host HOST | --all] VAULT [VAULT ...]

positional arguments:
  VAULT

{TARGETS}""",
    "status": f"""\
usage: {PROG} status [-h] [--host HOST | --all]

{TARGETS}""",
    "exec": f"""\
usage: {PROG} exec [-h] [--host HOST] [--operator] ...

positional arguments:
  command

options:
  -h, --help   show this help message and exit
  --host HOST  the host (default: this machine)
  --operator   run as yourself, after Touch ID
""",
}


@pytest.mark.usefixtures("plain")
@pytest.mark.parametrize("command", list(HELP))
def test_help_text(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [command, "--help"] if command else ["--help"]
    assert host_tool.main(argv, PROG) == 0
    assert capsys.readouterr() == (HELP[command], "")


@pytest.mark.usefixtures("plain")
def test_no_command_names_the_missing_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert host_tool.main([], PROG) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"usage: {PROG} [-h] {COMMANDS} ...\n"
        f"{PROG}: error: the following arguments are required: command_name\n"
    )


@pytest.mark.usefixtures("plain")
@pytest.mark.parametrize("command", ["bootstrap", "grant", "revoke", "status"])
def test_host_and_all_exclude_each_other(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert host_tool.main([command, "--host", "a", "--all", "v"], PROG) == 2
    err = capsys.readouterr().err
    assert err.endswith(
        f"{PROG} {command}: error: argument --all: not allowed with argument --host\n"
    )


RUNS = {
    "list": host_tool.cmd_list,
    "bootstrap": host_tool.cmd_bootstrap,
    "grant": host_tool.cmd_grant,
    "revoke": host_tool.cmd_revoke,
    "status": host_tool.cmd_status,
    "exec": host_tool.cmd_exec,
}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["list"], {}),
        (["bootstrap"], {"host": None, "all": False, "vaults": []}),
        (
            ["grant", "--host", "a", "--host", "b", "v", "w"],
            {"host": ["a", "b"], "all": False, "vaults": ["v", "w"]},
        ),
        (["revoke", "--all", "v"], {"host": None, "all": True, "vaults": ["v"]}),
        (["status"], {"host": None, "all": False}),
        (
            ["exec", "--host", "h", "--operator", "--", "a", "--b"],
            {"host": "h", "operator": True, "command": ["--", "a", "--b"]},
        ),
        (["exec", "a"], {"host": None, "operator": False, "command": ["a"]}),
    ],
)
def test_the_parsed_arguments(argv: list[str], expected: Json) -> None:
    args = vars(host_tool._parser(PROG).parse_args(argv))  # noqa: SLF001
    assert args == {"command_name": argv[0], "run": RUNS[argv[0]], **expected}


def test_parse_token_ignores_other_names(
    config: Callable[[list[str]], None],
) -> None:
    config(["https://connect-a.example.ts.net"])
    assert host_tool.parse_token({"id": "t", "name": "ci"}) is None
    assert host_tool.parse_token({"id": "t", "name": "example-host:h:bad:v"}) is None
    assert (
        host_tool.parse_token({"id": "t", "name": "other:h:20261104T120000Z:v"}) is None
    )
    token = host_tool.parse_token({"id": "t", "name": name_for("h", "a:b")})
    assert token is not None
    assert token.vaults == {"vault-all", "vault-h", "a:b"}
    assert token.created == T0


def test_now_is_utc() -> None:
    assert host_tool.now().tzinfo is UTC
