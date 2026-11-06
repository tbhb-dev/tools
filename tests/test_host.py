"""Tests for `tbhbdev host`.

They run against stub op, tailscale, ssh, sudo, security, secret-tool,
dbus-run-session, gnome-keyring-daemon and swift executables, and a fake
Connect API on 127.0.0.1 (see conftest.py). The stub ssh runs the remote
command here with HOME under tmp_path, so each host's agent keychain is a file
in the test's scratch directory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import shlex
import shutil
import stat
import subprocess  # nosec B404
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
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
# The Connect checks bootstrap makes per host: the new token's, then upgrade's
# token health check and its reads of the private gitleaks config, one for the
# agent and one for the operator.
BOOTSTRAP_CHECKS = 4
TAILNET = {
    "MagicDNSSuffix": SUFFIX,
    "CurrentTailnet": {"Name": "Fake Tailnet (example)"},
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
# Stand-ins for the private repositories, and the dictionary's words.
REPOS = {
    "example-org": ["fake-widget", "gadgets", ".fake-dotrepo"],
    "example-user": ["zq"],
}
WORDS = "Host\ngadget\nbox\nwidget\noperator\n"
# Stub ssh names a remote host's home after it; this machine's is "local".
HOME_KEY = {"host-a": "host-a", "host-b": "local", "host-c": "host-c"}
GID = str(host_tool.GID)
UID = str(host_tool.AGENT_UID)
MAC_DIR = (
    "group:devel allow list,add_file,search,add_subdirectory,delete_child,delete,"
    "readattr,writeattr,readextattr,writeextattr,readsecurity,"
    "file_inherit,directory_inherit"
)
MAC_FILE = (
    "group:devel allow read,write,append,execute,delete,readattr,writeattr,"
    "readextattr,writeextattr,readsecurity"
)
# A stand-in for the private gitleaks config, in the [vaults] all vault. No
# output may hold its marker.
GITLEAKS_MARKER = "stand-in-canary-5e1f"
GITLEAKS = (
    '[extend]\npath = ".gitleaks.toml"\n\n'
    '[[rules]]\nid = "tbhb-private-canary"\n'
    'description = "Stand-in canary"\n'
    f"regex = '''{GITLEAKS_MARKER}'''\n"
)
# Where upgrade installs it, below each account's home.
PRIVATE = ".config/gitleaks/private.toml"


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

    def operator_home(self, host: str) -> Path:
        return self.stubs.dir / "homes" / HOME_KEY[host] / "operator"

    def accounts(self, host: str) -> Json:
        hosts = cast("Json", self.stubs.state().get("directory", {}))
        return cast("Json", hosts.get(HOME_KEY[host], {"Users": {}, "Groups": {}}))

    def seed(
        self,
        host: str,
        users: dict[str, Json] | None = None,
        groups: dict[str, Json] | None = None,
    ) -> None:
        """Give HOST these account records before a test runs."""
        hosts = cast("Json", self.stubs.state().get("directory", {}))
        hosts[HOME_KEY[host]] = {"Users": users or {}, "Groups": groups or {}}
        self.stubs.set(directory=hosts)

    def acl(self, host: str, path: Path) -> list[str]:
        key = "acls_linux" if host == "host-c" else "acls_mac"
        acls = cast("dict[str, list[str]]", self.stubs.state().get(key, {}))
        return acls.get(str(path.resolve()), [])

    def root_scripts(self) -> list[str]:
        """The scripts bootstrap ran as root, unattended or after a password."""
        return [
            a[-1] for a in self.stubs.argvs("sudo") if a[:2] in (["-n", "sh"], ["sh"])
        ]

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
    fake.item("v1", "gitleaks-private", config=GITLEAKS)
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
    operator = stubs.dir / "homes" / "local" / "operator"
    operator.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(operator))

    def agent_home(host: host_tool.Host) -> str:
        return str(stubs.home(HOME_KEY[host.name]))

    monkeypatch.setattr(host_tool, "agent_home", agent_home)
    words = stubs.dir / "words"
    words.write_text(WORDS)
    monkeypatch.setattr(host_tool, "WORDS", words)
    stubs.set(tailscale=TAILNET, gh_repos=REPOS)
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
    # Only the stubs, so a real tailscale in /usr/bin can't answer instead.
    monkeypatch.setenv("PATH", str(world.stubs.bin))
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


def test_list_names_hosts_by_their_dns_name(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    tags = ["tag:example-agents"]
    world.stubs.set(
        tailscale={
            "Self": {"HostName": "laptop", "DNSName": "host-d.ts.net.", "Tags": tags},
            "Peer": {"n1": {"HostName": "host-e", "OS": "linux", "Tags": tags}},
        }
    )
    assert run(world, capfd, "list") == 0
    assert world.out == "host-d\t\t(this machine)\nhost-e\tlinux\n"


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
    assert credentials.parent.name.startswith("example-connect-")
    assert credentials.exists()
    assert world.checked == [ENDPOINTS] * BOOTSTRAP_CHECKS * len(HOSTS)
    assert credentials.parent.stat().st_mode & 0o777 == 0o700
    assert '"stub"' not in world.out
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
    reads = ("list", "item get")
    changes = [c for c in world.op_calls()[before:] if not c.endswith(reads)]
    assert changes == []
    assert f"gitleaks-generate: ok: {host_tool.gitleaks_ref()} is current" in world.out
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
        "created the vault ops\ngranted the Connect server the vault ops\n"
    )
    assert (
        f"host-a: the token {old['name']} has other vaults; creating a new token"
        in world.out.splitlines()
    )
    assert "host-a: config.toml ok" in world.out.splitlines()
    assert world.out.splitlines().index(
        "host-a: config.toml ok"
    ) < world.out.splitlines().index(
        f"host-a: the token {old['name']} has other vaults; creating a new token"
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
    urls = [f"https://{h}.{SUFFIX}" for h in labels]
    assert world.checked == [urls] * BOOTSTRAP_CHECKS


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
        "host-a: the new token can't list its vaults: "
        "Connect can't send GET /v1/vaults: DecodingError\n"
        f"{PROG}: bootstrap failed on host-a\n"
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
        '[vaults]\nall = "vault-all"\nhost_prefix = "vault-"\n\n'
        '[gitleaks]\nowners = ["example-org", "example-user"]\n'
        'session_sockets = ["/tmp/example-socks/"]\n'
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
        assert sorted(p.name for p in copy.parent.iterdir()) == [
            "config.toml",
            "connect-token-name",
        ]
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
        '[vaults]\nall = "vault-all"\nhost_prefix = "vault-"\n\n'
        '[gitleaks]\nowners = ["example-org", "example-user"]\n'
        'session_sockets = ["/tmp/example-socks/"]\n'
    )


def test_the_endpoints_follow_the_server_hosts(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    edit_config('["nas-a", "nas-b"]', '["nas-b", "nas-c", "nas-a"]')
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    copy = tomllib.loads(copy_of(world, "host-a").read_text())
    urls = [f"https://{h}.{SUFFIX}" for h in ("nas-b", "nas-c", "nas-a")]
    assert cast("Json", copy["connect"])["endpoints"] == urls
    assert world.checked == [urls] * BOOTSTRAP_CHECKS


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
    assert world.err == (
        "host-a: cannot install the agent's config.toml (exit 1)\n"
        f"{PROG}: bootstrap failed on host-a\n"
    )
    assert world.tokens() == []


def test_status_says_whether_each_copy_matches(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "--host", "host-c") == 0
    copy_of(world, "host-c").write_text(userconfig.path().read_text())
    assert run(world, capfd, "status", "--all") == 0
    lines = [line for line in world.out.splitlines() if "config.toml" in line]
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
    assert world.err == (
        f"host-a: installing the token failed (exit 44)\n"
        f"{PROG}: bootstrap failed on host-a\n"
    )
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
    assert world.err == f"host-a: {error}\n{PROG}: bootstrap failed on host-a\n"
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
        (
            {"ssh_down": ["host-a"]},
            "host-a: cannot find out who you are there: ssh: connect",
        ),
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
    assert run(world, capfd, "bootstrap", "--host", "phone", "--host", "host-a") == 1
    assert world.err == (
        f"phone: unsupported OS 'iOS'\n{PROG}: bootstrap failed on phone\n"
    )
    assert "host-phone" not in [v["name"] for v in world.rows("vaults")]
    assert world.token_of("host-a")["name"] == name_for("host-a")


def test_bootstrap_names_every_host_that_failed(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    tailnet = json.loads(json.dumps(TAILNET))
    tailnet["Peer"]["n4"]["Tags"] = ["tag:example-agents"]
    world.stubs.set(tailscale=tailnet, ssh_down=["host-c"])
    assert run(world, capfd, "bootstrap", "--host", "phone", "--host", "host-c") == 1
    assert world.err.endswith(f"{PROG}: bootstrap failed on phone, host-c\n")


def test_op_missing_from_path(
    world: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (world.stubs.bin / "op").unlink()
    # Only the stubs, so a real op in /usr/bin can't run instead.
    monkeypatch.setenv("PATH", str(world.stubs.bin))
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err == f"{PROG}: op not found\n"


# grant and revoke.


def test_grant_needs_a_server(world: World, capfd: pytest.CaptureFixture[str]) -> None:
    assert run(world, capfd, "grant", "--host", "host-a", "ops") == 1
    assert world.err == (
        f"{PROG}: there is no Connect server example-connect; run bootstrap first\n"
    )


def test_a_list_that_is_not_a_list_lists_nothing(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(op_raw={"connect server list": "null"})
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
    assert world.checked == [ENDPOINTS] * (BOOTSTRAP_CHECKS * len(HOSTS) + 1)
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


def test_grant_all_adds_a_vault_to_every_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all") == 0
    assert run(world, capfd, "grant", "--all", "extra", "more") == 1
    assert world.err == (
        f"{PROG}: no such vault: extra, more; run bootstrap to create it\n"
    )
    state = world.stubs.state()
    vaults = cast("list[Json]", cast("Json", state["op"])["vaults"])
    vaults.append({"id": "vlt-extra", "name": "extra"})
    world.stubs.set(**state)
    assert run(world, capfd, "grant", "--all", "extra") == 0
    for host in HOSTS:
        assert world.token_of(host)["name"] == name_for(host, "extra")


@pytest.mark.parametrize("vault", ["", "a,b"])
def test_vault_names_are_checked(
    world: World, capfd: pytest.CaptureFixture[str], vault: str
) -> None:
    assert run(world, capfd, "grant", "--all", vault) == 1
    assert "a vault name can't be empty or hold a comma" in world.err
    assert [c["tool"] for c in world.stubs.calls()] == ["swift"]


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


def test_revoke_of_a_vault_no_token_has_changes_nothing(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    before = world.op_calls()
    assert run(world, capfd, "revoke", "--host", "host-a", "ops") == 0
    assert "connect vault revoke" not in world.op_calls()[len(before) :]


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
    assert world.checked == [ENDPOINTS] * (BOOTSTRAP_CHECKS + 1) * len(HOSTS)
    world.assert_no_token_leaks()


def test_revoke_leaves_the_server_alone_beside_other_tokens(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "ops", "extra") == 0
    state = world.stubs.state()
    tokens = cast("list[Json]", cast("Json", state["op"])["tokens"])
    for name in ("ci", "example-host:x:not-a-time:ops"):
        tokens.append({**tokens[0], "id": f"tok-{name}", "name": name})
    world.stubs.set(**state)
    assert run(world, capfd, "revoke", "--host", "host-a", "ops", "extra") == 0
    assert world.out.endswith(
        "leaving the server's access to extra, ops alone: "
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
        "host-b: workspace access not configured\n"
    )


def test_status_shows_each_hosts_token_without_its_value(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a", "--host", "host-c") == 0
    assert run(world, capfd, "status", "--all") == 0
    assert world.out == (
        f"host-a: token {name_for('host-a')}; vaults vault-all, vault-host-a; "
        "expires 2027-02-02T12:00:00Z; keychain entry present; config.toml matches\n"
        "host-a: workspace access not configured\n"
        "host-b: no token; keychain entry absent; config.toml missing\n"
        "host-b: workspace access not configured\n"
        f"host-c: token {name_for('host-c')}; vaults vault-all, vault-host-c; "
        "expires 2027-02-02T12:00:00Z; keychain entry present; config.toml matches\n"
        "host-c: workspace access not configured\n"
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
    monkeypatch.setitem(host_tool.SCRIPTS["macOS"], "held", script)
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
    help_line = re.compile(
        r"^ +status +show hosts' tokens, configs and workspace access$", re.MULTILINE
    )
    assert help_line.search(capsys.readouterr().out)


COMMANDS = "{list,bootstrap,grant,revoke,upgrade,status,exec}"
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
    bootstrap           set up hosts as root, then upgrade them
    grant               add vaults to hosts' tokens
    revoke              take vaults from hosts' tokens
    upgrade             bring hosts up to date, unattended
    status              show hosts' tokens, configs and workspace access
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
usage: {PROG} bootstrap [-h] [--host HOST | --all] [--repair-acl]
                              [VAULT ...]

positional arguments:
  VAULT

options:
  -h, --help    show this help message and exit
  --host HOST   an agent host (repeatable)
  --all         every agent host
  --repair-acl  reapply the workspace ACL to everything below the root
""",
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
    "upgrade": f"""\
usage: {PROG} upgrade [-h] [--host HOST | --all]

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
@pytest.mark.parametrize(
    "command", ["bootstrap", "grant", "revoke", "upgrade", "status"]
)
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
    "upgrade": host_tool.cmd_upgrade,
    "status": host_tool.cmd_status,
    "exec": host_tool.cmd_exec,
}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["list"], {}),
        (
            ["bootstrap"],
            {"host": None, "all": False, "vaults": [], "repair_acl": False},
        ),
        (
            ["bootstrap", "--repair-acl"],
            {"host": None, "all": False, "vaults": [], "repair_acl": True},
        ),
        (
            ["grant", "--host", "a", "--host", "b", "v", "w"],
            {"host": ["a", "b"], "all": False, "vaults": ["v", "w"]},
        ),
        (["revoke", "--all", "v"], {"host": None, "all": True, "vaults": ["v"]}),
        (["upgrade", "--all"], {"host": None, "all": True}),
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


def in_order(out: str, lines: list[str]) -> bool:
    """Whether out holds lines, one after another."""
    return "\n".join(lines) in out


# Touch ID guards every command that acts as the operator.


@pytest.mark.parametrize(
    "command", [["bootstrap"], ["grant", "ops"], ["revoke", "ops"]]
)
@pytest.mark.parametrize(
    "case",
    [
        (1, "Darwin", "the Touch ID check failed"),
        (2, "Darwin", "{} needs Touch ID, which isn't available here"),
        (0, "Linux", "{} needs macOS with Touch ID"),
    ],
)
def test_touch_id_guards_bootstrap_grant_and_revoke(
    world: World,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    case: tuple[int, str, str],
) -> None:
    swift_rc, system, error = case
    world.stubs.set(swift_rc=swift_rc)
    monkeypatch.setattr(host_tool.platform, "system", lambda: system)
    assert run(world, capfd, *command, "--all") == 1
    assert world.err == f"{PROG}: {error.format(command[0])}\n"
    assert [c["tool"] for c in world.stubs.calls()] == (
        ["swift"] if system == "Darwin" else []
    )


def test_read_only_commands_and_upgrade_need_no_touch_id(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(swift_rc=1)
    assert run(world, capfd, "list") == 0
    assert run(world, capfd, "status", "--all") == 0
    assert run(world, capfd, "upgrade", "--all") == 1
    assert world.stubs.calls("swift") == []


# Accounts.


def user(uid: str = UID, gid: str = GID, home: str = "/Users/agent") -> Json:
    return {"UniqueID": [uid], "PrimaryGroupID": [gid], "NFSHomeDirectory": [home]}


def group(gid: str = GID, *members: str) -> Json:
    record: Json = {"PrimaryGroupID": [gid]}
    if members:
        record["GroupMembership"] = list(members)
    return record


def mac_create(path: str, *attributes: str) -> list[str]:
    return [
        (
            f"if dscl . -read {path} >/dev/null 2>&1; then echo '{path} appeared "
            "during bootstrap; leaving it alone' >&2; exit 1; fi"
        ),
        f"dscl . -create {path}",
        *(f"dscl . -create {path} {a}" for a in attributes),
    ]


def test_a_new_mac_host_gets_a_hidden_agent_without_a_password(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    home = world.stubs.home("host-a")
    accounts = world.accounts("host-a")
    assert accounts["Groups"] == {
        "devel": {
            "PrimaryGroupID": [GID],
            "RealName": ["devel"],
            "Password": ["*"],
            "GroupMembership": ["operator"],
        }
    }
    assert accounts["Users"] == {
        "agent": {
            "UniqueID": [UID],
            "PrimaryGroupID": [GID],
            "NFSHomeDirectory": [str(home)],
            "UserShell": ["/bin/zsh"],
            "RealName": ["agent"],
            "Password": ["*"],
            "IsHidden": ["1"],
        }
    }
    assert world.root_scripts() == [
        "\n".join(
            [
                "set -eu",
                *mac_create(
                    "/Groups/devel",
                    f"PrimaryGroupID {GID}",
                    "RealName devel",
                    "Password '*'",
                ),
                *mac_create(
                    "/Users/agent",
                    f"UniqueID {UID}",
                    f"PrimaryGroupID {GID}",
                    f"NFSHomeDirectory {home}",
                    "UserShell /bin/zsh",
                    "RealName agent",
                    "Password '*'",
                    "IsHidden 1",
                ),
                "dscl . -append /Groups/devel GroupMembership operator",
                f"install -d -o {UID} -g {GID} -m 0700 {home}",
            ]
        )
    ]
    assert home.stat().st_mode & 0o777 == 0o700
    assert cast("Json", world.stubs.state()["owners"]) == {str(home): f"{UID}:{GID}"}
    assert in_order(
        world.out,
        [
            "host-a: created the group devel with gid 33835",
            "host-a: created the user agent with uid 24368 and gid 33835",
            "host-a: added operator to the group devel",
            f"host-a: created the agent's home {home}",
        ],
    )


def test_a_new_linux_host_gets_a_locked_agent(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(operators={"host-c": "someone"})
    assert run(world, capfd, "bootstrap", "--host", "host-c") == 0
    home = world.stubs.home("host-c")
    accounts = world.accounts("host-c")
    assert accounts["Groups"] == {
        "devel": {"PrimaryGroupID": [GID], "GroupMembership": ["someone"]}
    }
    assert accounts["Users"] == {
        "agent": {
            "UniqueID": [UID],
            "PrimaryGroupID": [GID],
            "NFSHomeDirectory": [str(home)],
            "UserShell": ["/bin/bash"],
            "Password": ["!"],
        }
    }
    assert world.root_scripts() == [
        (
            "set -eu\n"
            f"groupadd --gid {GID} devel\n"
            f"useradd --uid {UID} --gid {GID} --create-home --home-dir {home} "
            "--shell /bin/bash agent\n"
            "usermod --lock agent\n"
            "usermod --append --groups devel someone\n"
            f"install -d -o {UID} -g {GID} -m 0700 {home}"
        )
    ]
    assert home.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("host", ["host-a", "host-c"])
def test_accounts_with_the_right_ids_are_left_alone(
    world: World, capfd: pytest.CaptureFixture[str], host: str
) -> None:
    home = world.stubs.home(HOME_KEY[host])
    home.mkdir(parents=True)
    home.chmod(0o750)
    seeded = (
        {"agent": user(home=str(home)), "other": user("501", "20")},
        {"devel": group(GID, "someone", "operator"), "staff": group("20")},
    )
    world.seed(host, *seeded)
    world.stubs.set(dscl_preamble=True)
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    assert in_order(
        world.out,
        [
            f"{host}: the group devel already has gid 33835",
            f"{host}: the user agent already has uid 24368 and gid 33835",
            f"{host}: operator is already in the group devel",
            f"{host}: the agent's home {home} already exists",
        ],
    )
    assert world.accounts(host) == {"Users": seeded[0], "Groups": seeded[1]}
    assert home.stat().st_mode & 0o777 == 0o750
    assert all(a[:4] == ["-n", "-u", "agent", "-H"] for a in world.stubs.argvs("sudo"))


@pytest.mark.parametrize(
    ("host", "command"),
    [
        ("host-a", "dscl . -append /Groups/devel GroupMembership operator"),
        ("host-c", "usermod --append --groups devel operator"),
    ],
)
def test_the_operator_joins_devel(
    world: World, capfd: pytest.CaptureFixture[str], host: str, command: str
) -> None:
    home = world.stubs.home(HOME_KEY[host])
    home.mkdir(parents=True)
    world.seed(host, {"agent": user(home=str(home))}, {"devel": group(GID, "x")})
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    assert world.root_scripts() == [f"set -eu\n{command}"]
    devel = cast("Json", cast("Json", world.accounts(host)["Groups"])["devel"])
    assert devel["GroupMembership"] == ["x", "operator"]
    assert f"{host}: added operator to the group devel" in world.out


@pytest.mark.parametrize(
    "case",
    [
        (
            "host-a",
            {"agent": user("550", "20")},
            {"devel": group()},
            (
                "the user agent has uid 550 and gid 20; "
                "it should have uid 24368 and gid 33835"
            ),
        ),
        (
            "host-c",
            {"agent": user("1001", "1003")},
            {"devel": group()},
            (
                "the user agent has uid 1001 and gid 1003; "
                "it should have uid 24368 and gid 33835"
            ),
        ),
        (
            "host-a",
            {"agent": user(UID, "20")},
            {"devel": group()},
            (
                "the user agent has uid 24368 and gid 20; "
                "it should have uid 24368 and gid 33835"
            ),
        ),
        (
            "host-c",
            {},
            {"devel": group("1003")},
            "the group devel has gid 1003; it should have 33835",
        ),
        ("host-a", {}, {"staff2": group()}, "gid 33835 belongs to staff2, not devel"),
        ("host-c", {}, {"staff2": group()}, "gid 33835 belongs to staff2, not devel"),
        ("host-a", {"someone": user()}, {}, "uid 24368 belongs to someone, not agent"),
        ("host-c", {"someone": user()}, {}, "uid 24368 belongs to someone, not agent"),
    ],
)
def test_wrong_or_taken_ids_stop_that_host_only(
    world: World,
    capfd: pytest.CaptureFixture[str],
    case: tuple[str, dict[str, Json], dict[str, Json], str],
) -> None:
    host, users, groups, error = case
    world.seed(host, users, groups)
    assert run(world, capfd, "bootstrap", "--host", host, "--host", "host-b") == 1
    assert world.err == f"{host}: {error}\n{PROG}: bootstrap failed on {host}\n"
    assert world.accounts(host) == {"Users": users, "Groups": groups}
    assert len(world.root_scripts()) == 1
    assert world.installed(host) is None
    assert world.installed("host-b") == world.token_of("host-b")["token"]


@pytest.mark.parametrize(
    "case",
    [
        ("host-a", {}, {"devel": group("x")}, "the group devel has no numeric id: 'x'"),
        (
            "host-a",
            {},
            {"devel": group("1:2")},
            "the group devel has no numeric id: '1:2'",
        ),
        ("host-a", {}, {"devel": {}}, "the group devel has no single PrimaryGroupID"),
        ("host-c", {}, {"devel": group("x")}, "the group devel has no numeric id: 'x'"),
        (
            "host-a",
            {"agent": user(gid="x")},
            {"devel": group()},
            "the user agent has no numeric id: 'x'",
        ),
        (
            "host-a",
            {"agent": {"UniqueID": [UID], "NFSHomeDirectory": ["/Users/agent"]}},
            {"devel": group()},
            "the user agent has no single PrimaryGroupID",
        ),
        (
            "host-a",
            {"agent": {"UniqueID": [UID], "PrimaryGroupID": [GID]}},
            {"devel": group()},
            "the user agent has no single NFSHomeDirectory",
        ),
        (
            "host-c",
            {"agent": user(uid="x")},
            {"devel": group()},
            "the user agent has no numeric id: 'x'",
        ),
    ],
)
def test_malformed_accounts_stop_that_host(
    world: World,
    capfd: pytest.CaptureFixture[str],
    case: tuple[str, dict[str, Json], dict[str, Json], str],
) -> None:
    host, users, groups, error = case
    world.seed(host, users, groups)
    assert run(world, capfd, "bootstrap", "--host", host) == 1
    assert world.err == f"{host}: {error}\n{PROG}: bootstrap failed on {host}\n"
    assert world.root_scripts() == []


@pytest.mark.parametrize(
    "case",
    [
        (
            "host-a",
            {"dscl_error": {"-read": "-14009 (eDSUnknownNodeName)"}},
            {},
            "cannot read /Groups/devel: <dscl_cmd> DS Error: -14009",
        ),
        (
            "host-a",
            {"dscl_error": {"-list": "-14009 (eDSUnknownNodeName)"}},
            {},
            "cannot read the UniqueIDs of /Users: <dscl_cmd> DS Error",
        ),
        ("host-c", {"getent_error": "nss broke"}, {}, "cannot read group devel: nss"),
        (
            "host-a",
            {},
            {"agent": {"UniqueID": ["a"], "PrimaryGroupID": [GID]}},
            "the user agent has no numeric id: 'a'",
        ),
        (
            "host-a",
            {},
            {"agent": {"PrimaryGroupID": [GID]}},
            "the user agent has no single UniqueID",
        ),
        (
            "host-c",
            {},
            {"agent": {"UniqueID": [UID], "PrimaryGroupID": ["-1"]}},
            "the user agent has no numeric id: '-1'",
        ),
    ],
)
def test_unreadable_accounts_stop_that_host(
    world: World,
    capfd: pytest.CaptureFixture[str],
    case: tuple[str, Json, dict[str, Json], str],
) -> None:
    host, state, users, error = case
    world.seed(host, users, {"devel": group(GID, "operator")})
    world.stubs.set(**state)
    assert run(world, capfd, "bootstrap", "--host", host) == 1
    assert world.err.startswith(f"{host}: {error}")
    assert world.root_scripts() == []


@pytest.mark.parametrize("host", ["host-a", "host-b"])
def test_sudo_asks_for_the_password_once_per_host(
    world: World, capfd: pytest.CaptureFixture[str], host: str
) -> None:
    world.stubs.set(sudo_password=True)
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    assert f"{host}: sudo needs your password\n" in world.err
    assert world.stubs.argvs("sudo-prompt") == [[HOME_KEY[host]]]
    assert world.accounts(host)["Users"] != {}
    if host == "host-a":
        tty = [a for a in world.stubs.argvs("ssh") if "-t" in a]
        assert len(tty) == 1
        assert tty[0][:4] == ["-o", "BatchMode=yes", "-t", "host-a"]
        assert tty[0][4].startswith("sudo sh -c 'set -eu\n")
    for argv in world.stubs.argvs("sudo"):
        assert not {"-S", "--stdin", "-A", "--askpass"} & set(argv)
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    assert len(world.stubs.argvs("sudo-prompt")) == 1


def test_a_wrong_sudo_password_stops_that_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(sudo_password=True, sudo_wrong_password=True)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err.endswith(
        "host-a: the setup as root failed (exit 1)\n"
        f"{PROG}: bootstrap failed on host-a\n"
    )
    assert world.accounts("host-a")["Users"] == {}


def test_a_failed_root_script_stops_that_host(
    world: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:

    def outside(_: host_tool.Host) -> str:
        return "/nonexistent/agent"

    monkeypatch.setattr(host_tool, "agent_home", outside)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err.startswith(
        "host-a: the setup as root failed: install stub: refusing /nonexistent/agent"
    )
    assert world.installed("host-a") is None


# Workspace access.


def add_to_config(config_file: Path, text: str) -> None:
    """Append TEXT, such as a [workspace] table, to the user config."""
    config_file.write_text(config_file.read_text() + text)


def with_workspace(
    config_file: Path, root: str = "Code", repos: tuple[str, ...] = ()
) -> None:
    text = f'[workspace]\nroot = "{root}"\nrepos = {json.dumps(list(repos))}\n'
    add_to_config(config_file, text)


def test_bootstrap_opens_the_workspace_to_devel_on_macos(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file, "src/work")
    home = world.operator_home("host-a")
    root = home / "src" / "work"
    (root / "repo" / "sub").mkdir(parents=True)
    (root / "repo" / "file").write_text("x")
    (root / "link").symlink_to(root / "repo" / "file")
    home.chmod(0o700)
    world.stubs.set(acls_mac={str(home): ["group:everyone deny delete"]})
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert world.acl("host-a", home) == [
        "group:everyone deny delete",
        "group:devel allow search",
    ]
    assert world.acl("host-a", home / "src") == ["group:devel allow search"]
    for directory in (root, root / "repo", root / "repo" / "sub"):
        assert world.acl("host-a", directory) == [MAC_DIR]
    assert world.acl("host-a", root / "repo" / "file") == [MAC_FILE]
    assert not any(str(root / "link") in a for a in world.stubs.argvs("chmod"))
    assert home.stat().st_mode & 0o777 == 0o700
    script = world.root_scripts()[0].splitlines()
    assert script[-6:] == [
        f"chmod +a 'group:devel allow search' {home}",
        f"chmod +a 'group:devel allow search' {home / 'src'}",
        f"find {root} -type d -exec chmod -a '{MAC_DIR}' {{}} + 2>/dev/null || :",
        f"find {root} -type d -exec chmod +a '{MAC_DIR}' {{}} +",
        (
            f"find {root} ! -type d ! -type l -exec chmod -a '{MAC_FILE}' {{}} + "
            "2>/dev/null || :"
        ),
        f"find {root} ! -type d ! -type l -exec chmod +a '{MAC_FILE}' {{}} +",
    ]
    lines = world.out.splitlines()
    assert f"host-a: let devel traverse {home}" in lines
    assert f"host-a: gave devel read, write and create on {root} and below" in lines
    assert f"host-a: the agent can read and write {root}" in lines
    assert any(a[-1].startswith(f"cd {root} && ") for a in world.stubs.argvs("sudo"))
    assert list(root.glob(".tbhbdev-probe*")) == []


def test_bootstrap_opens_the_workspace_to_devel_on_linux(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file)
    home = world.operator_home("host-c")
    root = home / "Code"
    (root / "sub").mkdir(parents=True)
    (root / "plain").write_text("x")
    (root / "tool").write_text("#!/bin/sh\n")
    (root / "tool").chmod(0o755)
    (root / "link").symlink_to(root / "plain")
    assert run(world, capfd, "bootstrap", "--host", "host-c") == 0
    assert world.acl("host-c", home) == ["group:devel:--x"]
    for directory in (root, root / "sub"):
        assert world.acl("host-c", directory) == [
            "group:devel:rwx",
            "default:group:devel:rwx",
        ]
    assert world.acl("host-c", root / "plain") == ["group:devel:rw-"]
    assert world.acl("host-c", root / "tool") == ["group:devel:rwx"]
    assert world.root_scripts()[0].splitlines()[-3:] == [
        f"setfacl -m group:devel:--x {home}",
        f"setfacl -R -P -m group:devel:rwX {root}",
        f"find {root} -type d -exec setfacl -m default:group:devel:rwx {{}} +",
    ]
    assert f"host-c: created {root}" not in world.out


@pytest.mark.parametrize("host", ["host-a", "host-c"])
def test_present_entries_are_not_added_again(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str], host: str
) -> None:
    with_workspace(config_file)
    root = world.operator_home(host) / "Code"
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    assert f"{host}: created {root}" in world.out
    before = world.stubs.state()
    scripts = len(world.root_scripts())
    (root / "new").write_text("x")
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    assert f"{host}: devel already has access to {root}" in world.out
    assert len(world.root_scripts()) == scripts
    key = "acls_linux" if host == "host-c" else "acls_mac"
    assert world.stubs.state()[key] == before[key]
    assert world.acl(host, root / "new") == []


@pytest.mark.parametrize("host", ["host-a", "host-c"])
def test_repair_acl_reapplies_below_the_root_without_duplicates(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str], host: str
) -> None:
    with_workspace(config_file)
    root = world.operator_home(host) / "Code"
    (root / "old").mkdir(parents=True)
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    (root / "new").write_text("x")
    assert run(world, capfd, "bootstrap", "--host", host, "--repair-acl") == 0
    file_entry = ["group:devel:rw-"] if host == "host-c" else [MAC_FILE]
    dir_entry = (
        ["group:devel:rwx", "default:group:devel:rwx"]
        if host == "host-c"
        else [MAC_DIR]
    )
    assert world.acl(host, root / "new") == file_entry
    assert world.acl(host, root / "old") == dir_entry
    assert world.acl(host, root) == dir_entry
    assert len(world.acl(host, root.parent)) == 1


def test_an_inherited_entry_counts_as_present(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file)
    home = world.operator_home("host-a")
    root = home / "Code"
    root.mkdir(parents=True)
    world.stubs.set(
        acls_mac={
            str(home): ["group:devel inherited allow list,search"],
            str(root): [MAC_DIR.replace(" allow", " inherited allow")],
        }
    )
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert f"host-a: devel already has access to {root}" in world.out
    assert world.acl("host-a", home) == ["group:devel inherited allow list,search"]


def test_rights_split_across_entries_count_together(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file)
    home = world.operator_home("host-a")
    root = home / "Code"
    root.mkdir(parents=True)
    inherit = ",file_inherit,directory_inherit"
    acls = {
        str(home): ["group:devel allow search", "group:devel allow list"],
        str(root): [MAC_DIR.removesuffix(inherit), f"group:devel allow {inherit[1:]}"],
    }
    world.stubs.set(acls_mac=acls)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert f"host-a: devel already has access to {root}" in world.out
    assert world.stubs.state()["acls_mac"] == acls


def test_a_partial_root_entry_is_completed(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file)
    home = world.operator_home("host-a")
    root = home / "Code"
    root.mkdir(parents=True)
    partial = "group:devel allow list,search"
    world.stubs.set(acls_mac={str(home): [partial], str(root): [partial]})
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert world.acl("host-a", root) == [partial, MAC_DIR]
    assert world.acl("host-a", home) == [partial]


@pytest.mark.parametrize(
    "case",
    [
        ("host-a", {"ls_error": "boom"}, "cannot read the ACL of {home}: ls: boom"),
        (
            "host-c",
            {"getfacl_error": "boom"},
            "cannot read the ACL of {home}: getfacl: boom",
        ),
        (
            "host-a",
            {"probe_denied": True},
            "the agent can't read and write {home}/Code: ls: .: Permission denied",
        ),
    ],
)
def test_workspace_failures_stop_that_host(
    world: World,
    config_file: Path,
    capfd: pytest.CaptureFixture[str],
    case: tuple[str, Json, str],
) -> None:
    host, state, error = case
    with_workspace(config_file)
    world.stubs.set(**state)
    assert run(world, capfd, "bootstrap", "--host", host) == 1
    home = world.operator_home(host)
    assert world.err == (
        f"{host}: {error.format(home=home)}\n{PROG}: bootstrap failed on {host}\n"
    )
    assert world.installed(host) is None
    assert list((home / "Code").glob(".tbhbdev-probe*")) == []


def test_a_root_that_cannot_be_made_stops_that_host(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file)
    home = world.operator_home("host-a")
    home.mkdir(parents=True)
    (home / "Code").write_text("not a directory")
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err.startswith(f"host-a: cannot create {home / 'Code'}: mkdir: ")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[workspace\n", "cannot parse "),
        ('[workspace]\nroot = "/abs"\n', "must be a relative path below home"),
        ('[workspace]\nroot = "a/../b"\n', "must be a relative path below home"),
        ('[workspace]\nroot = "."\n', "must be a relative path below home"),
        ("[workspace]\nroot = 1\n", "must be a relative path below home"),
        ("[workspace]\n", "must be a relative path below home"),
        ('[workspace]\nroot = "c"\nrepos = "o/n"\n', "repos must list owner/name"),
        ('[workspace]\nroot = "c"\nrepos = ""\n', "repos must list owner/name"),
        ('[workspace]\nroot = "c"\nrepos = ["o"]\n', "repos must list owner/name"),
        ('[workspace]\nroot = "c"\nrepos = ["o/n/x"]\n', "repos must list owner/name"),
        ('[workspace]\nroot = "c"\nrepos = ["o/n", 1]\n', "repos must list owner/"),
        ('[workspace]\nroot = "c"\nrepos = ["../n"]\n', "repos must list owner/name"),
    ],
)
def test_the_workspace_config_is_checked(
    world: World,
    config_file: Path,
    capfd: pytest.CaptureFixture[str],
    text: str,
    error: str,
) -> None:
    add_to_config(config_file, text)
    assert run(world, capfd, "upgrade") == 1
    assert world.err.startswith(f"{PROG}: ")
    assert error in world.err
    assert world.stubs.calls("ssh") == []


def test_a_workspace_needs_no_repos(config_file: Path) -> None:
    config_file.write_text('[workspace]\nroot = "c"\n')
    assert host_tool.workspace() == host_tool.Workspace(PurePosixPath("c"), ())


def test_a_workspace_that_is_not_a_table_is_refused(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text('workspace = "Code"\n' + config_file.read_text())
    assert run(world, capfd, "upgrade") == 1
    assert world.err == f"{PROG}: {config_file}: workspace must be a table\n"


def test_a_config_without_a_workspace_skips_it(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    add_to_config(config_file, '[other]\nx = "1"\n')
    assert run(world, capfd, "bootstrap", "--host", "host-b") == 0
    assert host_tool.NO_WORKSPACE in world.out.splitlines()
    assert "workspace access" not in world.out.replace(host_tool.NO_WORKSPACE, "")


@pytest.mark.parametrize("host", ["host-a", "host-c"])
def test_status_shows_workspace_access(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str], host: str
) -> None:
    with_workspace(config_file, "src/work")
    home = world.operator_home(host)
    root = home / "src" / "work"
    assert run(world, capfd, "status", "--host", host) == 0
    assert world.out.endswith(f"{host}: workspace access {root} doesn't exist\n")
    root.mkdir(parents=True)
    assert run(world, capfd, "status", "--host", host) == 0
    assert world.out.endswith(
        f"{host}: workspace access devel is missing entries on "
        f"{home}, {home / 'src'}, {root}\n"
    )
    assert run(world, capfd, "bootstrap", "--host", host) == 0
    assert run(world, capfd, "status", "--host", host) == 0
    assert world.out.endswith(
        f"{host}: workspace access complete for devel on {root}\n"
    )


# The token's recorded name.


def token_name_file(world: World, host: str) -> Path:
    return (
        world.stubs.home(HOME_KEY[host])
        / ".config"
        / "tbhbdev"
        / ("connect-token-name")
    )


@pytest.mark.parametrize(
    ("recorded", "why"),
    [(None, "an unrecorded token"), ("example-host:old", "example-host:old")],
)
def test_a_keychain_with_another_token_gets_a_new_one(
    world: World, capfd: pytest.CaptureFixture[str], recorded: str | None, why: str
) -> None:
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    name = name_for("host-a")
    path = token_name_file(world, "host-a")
    assert path.read_text() == f"{name}\n"
    if recorded is None:
        path.unlink()
    else:
        path.write_text(f"{recorded}\n")
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert (
        f"host-a: the keychain holds {why}, not {name}; creating a new token"
        in world.out
    )
    assert path.read_text() == f"{name}\n"
    assert world.installed("host-a") == world.token_of("host-a")["token"]


# upgrade.


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()  # noqa: S324


def remote(world: World, repo: str, *labels: str, default: str = "main") -> None:
    remotes = cast("Json", world.stubs.state().get("remotes", {}))
    remotes[repo] = {"default": default, "history": [sha(x) for x in labels]}
    world.stubs.set(remotes=remotes)


def clone_of(world: World, host: str, repo: str) -> Path:
    return world.operator_home(host) / "Code" / repo / ".git"


def change_clone(world: World, host: str, repo: str, **values: object) -> None:
    path = clone_of(world, host, repo)
    path.write_text(json.dumps({**json.loads(path.read_text()), **values}))


def upgrade_lines(world: World, host: str = "host-b") -> list[str]:
    return [line for line in world.out.splitlines() if line.startswith(f"{host}: ")]


@pytest.fixture
def ready(world: World, config_file: Path, capfd: pytest.CaptureFixture[str]) -> World:
    """host-b, bootstrapped with a workspace that clones example/app."""
    with_workspace(config_file, repos=("example/app",))
    remote(world, "example/app", "a1")
    assert run(world, capfd, "bootstrap", "--host", "host-b") == 0
    return world


def test_bootstrap_ends_by_upgrading(ready: World) -> None:
    root = ready.operator_home("host-b") / "Code"
    assert upgrade_lines(ready)[-5:] == [
        f"host-b: clone example/app: changed: cloned into {root / 'example/app'}",
        f"host-b: tools: ok: no mise config in {root}",
        (
            f"host-b: token: ok: the token {name_for('host-b')} reaches its vaults; "
            "it expires 2027-02-02"
        ),
        f"host-b: gitleaks-config agent: changed: wrote {PRIVATE}",
        f"host-b: gitleaks-config operator: changed: wrote {PRIVATE}",
    ]
    clone = json.loads(clone_of(ready, "host-b", "example/app").read_text())
    assert clone["head"] == sha("a1")


def test_upgrade_again_changes_nothing(
    ready: World, capfd: pytest.CaptureFixture[str]
) -> None:
    before = len(ready.stubs.calls())
    assert run(ready, capfd, "upgrade") == 0
    assert ready.out.splitlines()[0] == (
        f"host-b: clone example/app: ok: up to date at {sha('a1')[:12]}"
    )
    calls = ready.stubs.calls()[before:]
    tools = {c["tool"] for c in calls}
    assert not tools & {"swift", "op", "dscl", "chmod", "install", "sudo-prompt"}
    for call in calls:
        argv = cast("list[str]", call["argv"])
        if call["tool"] == "sudo":
            assert argv[:4] == ["-n", "-u", "agent", "-H"]
        assert "-t" not in argv or call["tool"] != "ssh"
    ready.assert_no_token_leaks()


def test_upgrade_fast_forwards_a_clean_clone(
    ready: World, capfd: pytest.CaptureFixture[str]
) -> None:
    remote(ready, "example/app", "a1", "a2")
    assert run(ready, capfd, "upgrade", "--host", "host-b") == 0
    assert ready.out.splitlines()[0] == (
        "host-b: clone example/app: changed: fast-forwarded main from "
        f"{sha('a1')[:12]} to {sha('a2')[:12]}"
    )
    clone = json.loads(clone_of(ready, "host-b", "example/app").read_text())
    assert clone["head"] == sha("a2")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"branch": "topic"}, "left alone: on topic, not main"),
        ({"branch": None}, "left alone: on a detached HEAD, not main"),
        ({"dirty": True}, "left alone: tracked files are modified or staged"),
        ({"head": sha("mine")}, "left alone: main has commits origin/main lacks"),
    ],
)
def test_upgrade_leaves_other_work_alone(
    ready: World, capfd: pytest.CaptureFixture[str], change: Json, message: str
) -> None:
    remote(ready, "example/app", "a1", "a2")
    change_clone(ready, "host-b", "example/app", **change)
    before = clone_of(ready, "host-b", "example/app").read_text()
    assert run(ready, capfd, "upgrade") == 0
    assert ready.out.splitlines()[0] == f"host-b: clone example/app: ok: {message}"
    after = json.loads(clone_of(ready, "host-b", "example/app").read_text())
    assert after["head"] == json.loads(before)["head"]


@pytest.mark.parametrize(
    ("change", "state", "message"),
    [
        ({"no_origin_head": True}, {}, "cannot find the default branch: fatal: amb"),
        ({"origin_head": "origin/"}, {}, "cannot find the default branch: exit 0"),
        ({"status_broken": True}, {}, "git status failed: fatal: index file corrupt"),
        ({}, {"fetch_fail": ["example/app"]}, "the fetch failed: fatal: could not"),
        ({"unresolved": True}, {}, "cannot resolve HEAD and origin/main"),
        ({"merge_fail": True}, {}, "the fast-forward failed: fatal: Not possible"),
    ],
)
def test_upgrade_reports_a_failed_clone_step(
    ready: World,
    capfd: pytest.CaptureFixture[str],
    change: Json,
    state: Json,
    message: str,
) -> None:
    remote(ready, "example/app", "a1", "a2")
    change_clone(ready, "host-b", "example/app", **change)
    ready.stubs.set(**state)
    assert run(ready, capfd, "upgrade") == 1
    assert ready.out.splitlines()[0].startswith(
        f"host-b: clone example/app: failed: {message}"
    )
    assert ready.err == f"{PROG}: upgrade failed on host-b\n"


def test_upgrade_reports_a_failed_clone(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file, repos=("example/app", "example/gone", "example/dir"))
    remote(world, "example/app", "a1")
    remote(world, "example/dir", "d1")
    (world.operator_home("host-b") / "Code" / "example" / "dir").mkdir(parents=True)
    assert run(world, capfd, "bootstrap", "--host", "host-b") == 1
    lines = upgrade_lines(world)
    assert lines[-7].startswith("host-b: clone example/app: changed: cloned into")
    assert lines[-6] == (
        "host-b: clone example/gone: failed: the clone failed: GraphQL: Could not "
        "resolve to a Repository with the name 'example/gone'."
    )
    assert lines[-5].startswith(
        "host-b: clone example/dir: failed: cannot find the default branch: "
        "fatal: not a git repository"
    )
    assert world.err == f"{PROG}: bootstrap failed on host-b\n"
    world.stubs.set(clone_fail=True)
    shutil.rmtree(world.operator_home("host-b") / "Code" / "example" / "app")
    assert run(world, capfd, "upgrade") == 1
    assert upgrade_lines(world)[0] == (
        "host-b: clone example/app: failed: the clone failed: "
        "fatal: unable to access the repository"
    )


@pytest.mark.parametrize(
    ("mise_config", "state", "line"),
    [
        (None, {}, "ok: no mise config in {root}"),
        ("mise.toml", {}, "ok: every tool is installed"),
        (".config/mise/config.toml", {}, "ok: every tool is installed"),
        (
            ".tool-versions",
            {"mise_missing": True},
            "changed: installed the missing tools",
        ),
        (
            "mise.toml",
            {"mise_missing": True, "mise_broken": True},
            "failed: mise install failed: mise ERROR failed to install tools",
        ),
    ],
)
def test_upgrade_installs_the_workspace_tools(
    ready: World,
    capfd: pytest.CaptureFixture[str],
    mise_config: str | None,
    state: Json,
    line: str,
) -> None:
    root = ready.operator_home("host-b") / "Code"
    if mise_config is not None:
        (root / mise_config).parent.mkdir(parents=True, exist_ok=True)
        (root / mise_config).write_text("")
    ready.stubs.set(**state)
    code = run(ready, capfd, "upgrade")
    assert code == (1 if "failed" in line else 0)
    assert upgrade_lines(ready)[1] == f"host-b: tools: {line.format(root=root)}"
    if "changed" in line:
        assert ready.stubs.state()["mise_cwd"] == str(root)
        assert run(ready, capfd, "upgrade") == 0
        assert upgrade_lines(ready)[1] == "host-b: tools: ok: every tool is installed"


HINT = "run `tbhbdev host bootstrap --host host-b`"


@pytest.mark.parametrize(
    "fault", ["no entry", "no name", "expiring", "unreachable", "wrong vaults"]
)
def test_upgrade_reports_an_unhealthy_token(
    ready: World,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    name = name_for("host-b")
    keychain = ready.stubs.home() / "Library" / "Keychains"
    message = {
        "no entry": f"the agent keychain has no token; {HINT}",
        "no name": f"the keychain's token has no recorded name; {HINT}",
        "expiring": f"the token {name} expires 2027-02-02; {HINT}",
        "unreachable": (
            f"the token {name} can't list its vaults: no Connect endpoint answered "
            f"GET /v1/vaults; {HINT}"
        ),
        "wrong vaults": (
            f"the token {name} lists ['vault-all'], not ['vault-all', 'vault-host-b']; "
            f"{HINT}"
        ),
    }[fault]
    if fault == "no entry":
        (keychain / f"{secrets.KEYCHAIN}-db").unlink()
    elif fault == "no name":
        token_name_file(ready, "host-b").unlink()
    elif fault == "expiring":
        monkeypatch.setattr(host_tool, "now", lambda: T0 + timedelta(days=76))
    elif fault == "unreachable":
        ready.fake.status = 503
    else:
        ready.fake.vaults = {
            k: v for k, v in ready.fake.vaults.items() if v == "vault-all"
        }
    before = ready.op_calls()
    assert run(ready, capfd, "upgrade") == 1
    assert upgrade_lines(ready)[-3] == f"host-b: token: failed: {message}"
    assert ready.op_calls() == before
    ready.assert_no_token_leaks()


def test_a_failed_keychain_check_is_an_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(host_tool.SCRIPTS["macOS"], "held", "echo present; exit 3")
    with pytest.raises(host_tool.HostError, match="cannot check the agent keychain"):
        host_tool.keychain_held(host_tool.Host("host-b", "macOS", local=True))
    assert world.stubs.argvs("sudo")[-1][-1] == "echo present; exit 3"


def test_an_empty_keychain_entry_has_no_token(
    ready: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    read = f"printf '%s\\n\\n' {name_for('host-b')}"
    monkeypatch.setitem(host_tool.SCRIPTS["macOS"], "read", read)
    assert run(ready, capfd, "upgrade") == 1
    assert upgrade_lines(ready)[-3] == (
        f"host-b: token: failed: the agent keychain has no token; {HINT}"
    )


def test_a_token_well_inside_its_lifetime_is_healthy(
    ready: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_tool, "now", lambda: T0 + timedelta(days=75))
    assert run(ready, capfd, "upgrade") == 0
    assert upgrade_lines(ready)[-3].startswith("host-b: token: ok: ")


def test_upgrade_carries_on_past_an_unreachable_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all") == 0
    world.stubs.set(ssh_down=["host-a", "host-c"])
    assert run(world, capfd, "upgrade", "--all") == 1
    assert world.err.startswith(
        "host-a: cannot find out who you are there: ssh: connect to host host-a"
    )
    assert world.err.endswith(f"{PROG}: upgrade failed on host-a, host-c\n")
    assert [line.split(": ")[0] for line in world.out.splitlines()] == [
        host_tool.NO_WORKSPACE.split(": ")[0],
        *["host-b"] * 3,
    ]


def test_upgrade_refuses_a_host_with_another_os(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    tailnet = json.loads(json.dumps(TAILNET))
    tailnet["Peer"]["n4"]["Tags"] = ["tag:example-agents"]
    world.stubs.set(tailscale=tailnet)
    assert run(world, capfd, "upgrade", "--host", "phone") == 1
    assert (
        world.err == f"phone: unsupported OS 'iOS'\n{PROG}: upgrade failed on phone\n"
    )


def test_a_step_that_raises_fails_alone(
    ready: World, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(target: host_tool.Target) -> host_tool.Result:
        message = f"{target.host.name}: boom"
        raise host_tool.HostError(message)

    monkeypatch.setattr(host_tool, "tools", broken)
    assert run(ready, capfd, "upgrade") == 1
    assert upgrade_lines(ready)[1:] == [
        "host-b: tools: failed: boom",
        (
            f"host-b: token: ok: the token {name_for('host-b')} reaches its vaults; "
            "it expires 2027-02-02"
        ),
        f"host-b: gitleaks-config agent: ok: {PRIVATE} is current",
        f"host-b: gitleaks-config operator: ok: {PRIVATE} is current",
    ]


def test_a_second_bootstrap_and_upgrade_change_nothing_anywhere(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file, repos=("example/app",))
    remote(world, "example/app", "a1")
    (world.operator_home("host-b") / "Code").mkdir(parents=True)
    (world.operator_home("host-b") / "Code" / "mise.toml").write_text("")
    world.stubs.set(mise_missing=True)
    assert run(world, capfd, "bootstrap", "--all") == 0
    first = world.out
    assert "host-b: tools: changed: installed the missing tools" in first
    scripts = world.root_scripts()
    assert len(scripts) == len(HOSTS)
    state = world.stubs.state()
    op_before = world.op_calls()
    for argv in (["bootstrap", "--all"], ["upgrade", "--all"]):
        assert run(world, capfd, *argv) == 0
        assert world.root_scripts() == scripts
        for word in (": changed:", "created", "added", "gave", "let ", "installed the"):
            assert word not in world.out, (argv, word)
        later = world.stubs.state()
        for key in ("directory", "acls_mac", "acls_linux", "owners"):
            assert later[key] == state[key]
    later_calls = world.op_calls()[len(op_before) :]
    changes = [c for c in later_calls if not c.endswith(("list", "item get"))]
    assert changes == []
    world.assert_no_token_leaks()


def test_a_keychain_that_cannot_be_made_stops_that_host(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    home = world.stubs.home("local")
    home.mkdir(parents=True)
    (home / "Library").write_text("not a directory")
    assert run(world, capfd, "bootstrap", "--host", "host-b") == 1
    assert world.err.startswith("host-b: cannot set up the agent keychain: mkdir: ")
    assert world.installed("host-b") is None


@pytest.mark.parametrize(
    "state",
    [
        {"operators": {"local": "two\nlines"}},
        {"operators": {"local": "three\n/lines"}},
        {"operators": {"local": "op\n/home"}, "id_fails": True},
    ],
)
def test_an_odd_answer_to_who_am_i_stops_that_host(
    world: World, capfd: pytest.CaptureFixture[str], state: Json
) -> None:
    world.stubs.set(**state)
    assert run(world, capfd, "upgrade", "--host", "host-b") == 1
    assert world.err.startswith("host-b: cannot find out who you are there")


def test_status_refuses_a_host_with_another_os(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    tailnet = json.loads(json.dumps(TAILNET))
    tailnet["Peer"]["n4"]["Tags"] = ["tag:example-agents"]
    world.stubs.set(tailscale=tailnet)
    assert run(world, capfd, "status", "--host", "phone") == 1
    assert world.err == f"{PROG}: phone: unsupported OS 'iOS'\n"


@pytest.mark.parametrize(
    ("os", "home"), [("macOS", "/Users/agent"), ("linux", "/home/agent")]
)
def test_the_agent_home_follows_the_os(os: str, home: str) -> None:
    assert host_tool.agent_home(host_tool.Host("h", os, local=False)) == home


# The private gitleaks config.


@pytest.fixture
def configured(config: Callable[[list[str] | None], None]) -> None:
    """The example user config, which the rules and the config check read."""
    config(None)


ACCOUNTS = ("agent", "operator")
REF = "op://vault-all/gitleaks-private/config"
CANARY_WHY = f"{REF} has no rule with the id tbhb-private-canary"
EXTEND_WHY = f'{REF} has no [extend] path = ".gitleaks.toml"'
CURRENT = f"ok: {PRIVATE} is current"
WROTE = f"changed: wrote {PRIVATE}"


def private_config(world: World, account: str, host: str = "host-b") -> Path:
    if account == "agent":
        return world.stubs.home(HOME_KEY[host]) / PRIVATE
    return world.operator_home(host) / PRIVATE


def private_fields(world: World) -> list[Json]:
    """The fields of the stand-in item in the [vaults] all vault."""
    return cast("list[Json]", world.fake.items["v1"]["i1"]["fields"])


def gitleaks_lines(world: World, host: str = "host-b") -> dict[str, str]:
    """Each account's gitleaks-config report on HOST: its status and message."""
    prefix = f"{host}: gitleaks-config "
    found: dict[str, str] = {}
    for line in upgrade_lines(world, host):
        if line.startswith(prefix):
            account, _, rest = line.removeprefix(prefix).partition(": ")
            found[account] = rest
    return found


def assert_installed(world: World, account: str, host: str = "host-b") -> None:
    """ACCOUNT's copy is the config, at 0600, alone in a 0700 directory."""
    path = private_config(world, account, host)
    assert path.read_bytes() == GITLEAKS.encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["private.toml"]


def assert_no_config_leaks(world: World) -> None:
    """The config in no argv, logged stdin, output or error of the last run."""
    for text in (json.dumps(world.stubs.calls()), world.out, world.err):
        assert GITLEAKS_MARKER not in text


def test_upgrade_installs_the_private_gitleaks_config_for_both_accounts(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    assert run(world, capfd, "bootstrap", "--all") == 0
    assert_no_config_leaks(world)
    for host in HOSTS:
        assert gitleaks_lines(world, host) == dict.fromkeys(ACCOUNTS, WROTE)
        for account in ACCOUNTS:
            assert_installed(world, account, host)
        agent, operator = (private_config(world, a, host) for a in ACCOUNTS)
        assert not agent.samefile(operator)
    # The operator's copy goes over ssh to a plain sh, the agent's through sudo.
    sh = ["sh", "-c", host_tool.GITLEAKS_INSTALL]
    commands = (shlex.join(["sudo", "-n", "-u", "agent", "-H", *sh]), shlex.join(sh))
    installs = [a for a in world.stubs.argvs("ssh") if a[-1] in commands]
    assert sorted(installs) == sorted(
        ["-o", "BatchMode=yes", host, command]
        for host in ("host-a", "host-c")
        for command in commands
    )
    assert run(world, capfd, "upgrade", "--all") == 0
    for host in HOSTS:
        assert gitleaks_lines(world, host) == dict.fromkeys(ACCOUNTS, CURRENT)
    assert_no_config_leaks(world)


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize(
    ("fault", "report"),
    [
        ("identical", CURRENT),
        ("missing", WROTE),
        ("no directory", WROTE),
        ("different", WROTE),
        ("same size", WROTE),
        ("file mode", WROTE),
        ("directory mode", WROTE),
    ],
)
def test_upgrade_puts_each_copy_right(
    ready: World,
    capfd: pytest.CaptureFixture[str],
    account: str,
    fault: str,
    report: str,
) -> None:
    path = private_config(ready, account)
    if fault == "missing":
        path.unlink()
    elif fault == "no directory":
        shutil.rmtree(path.parent)
    elif fault == "different":
        path.write_text(f"{GITLEAKS}# edited\n")
    elif fault == "same size":
        path.write_text(GITLEAKS.replace("5e1f", "5e1e"))
    elif fault == "file mode":
        path.chmod(0o644)
    elif fault == "directory mode":
        path.parent.chmod(0o755)
    assert run(ready, capfd, "upgrade") == 0
    other = next(a for a in ACCOUNTS if a != account)
    assert gitleaks_lines(ready) == {account: report, other: CURRENT}
    for each in ACCOUNTS:
        assert_installed(ready, each)
    assert_no_config_leaks(ready)
    assert run(ready, capfd, "upgrade") == 0
    assert gitleaks_lines(ready) == dict.fromkeys(ACCOUNTS, CURRENT)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (GITLEAKS, None),
        (
            GITLEAKS.replace(
                'id = "tbhb-private-canary"', "  id='tbhb-private-canary'  # canary"
            ),
            None,
        ),
        (GITLEAKS.replace('id = "', 'id = "x-'), CANARY_WHY),
        (GITLEAKS.replace('canary"', 'canary-2"'), CANARY_WHY),
        (GITLEAKS.replace('id = "', '# id = "'), CANARY_WHY),
        (GITLEAKS.replace('[extend]\npath = ".gitleaks.toml"\n', ""), EXTEND_WHY),
        (GITLEAKS.replace('path = ".gitleaks.toml"', 'path = "x.toml"'), EXTEND_WHY),
        (GITLEAKS.replace("[extend]\npath", "extend"), EXTEND_WHY),
        (f"{GITLEAKS}[[[\n", f"{REF} isn't valid TOML"),
    ],
)
@pytest.mark.usefixtures("configured")
def test_the_private_config_check(text: str, why: str | None) -> None:
    assert host_tool.gitleaks_problem(text) == why


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (GITLEAKS.replace('id = "', 'id = "x-'), CANARY_WHY),
        (GITLEAKS.replace('[extend]\npath = ".gitleaks.toml"\n', ""), EXTEND_WHY),
        (f"{GITLEAKS}[[[\n", f"{REF} isn't valid TOML"),
    ],
)
def test_a_config_that_fails_its_check_is_not_written(
    ready: World, capfd: pytest.CaptureFixture[str], text: str, why: str
) -> None:
    agent, operator = (private_config(ready, a) for a in ACCOUNTS)
    shutil.rmtree(agent.parent)
    operator.write_text("older\n")
    private_fields(ready)[0]["value"] = text
    assert run(ready, capfd, "upgrade") == 1
    assert gitleaks_lines(ready) == dict.fromkeys(
        ACCOUNTS, f"failed: {why}; wrote nothing"
    )
    assert not agent.parent.exists()
    assert operator.read_text() == "older\n"
    assert [p.name for p in operator.parent.iterdir()] == ["private.toml"]
    assert ready.err == f"{PROG}: upgrade failed on host-b\n"
    assert_no_config_leaks(ready)


@pytest.mark.parametrize(
    "fault", ["unreachable", "no item", "no field", "no token", "stray name"]
)
def test_a_config_that_cannot_be_read_is_not_written(
    ready: World, capfd: pytest.CaptureFixture[str], fault: str
) -> None:
    cannot = f"cannot read {REF}: "
    why = {
        "unreachable": f"{cannot}no Connect endpoint answered GET /v1/vaults",
        "no item": f"{cannot}no item 'gitleaks-private' in vault 'vault-all'",
        "no field": f"{cannot}no field 'config' in item 'gitleaks-private'",
        "no token": f"the agent keychain has no token; {HINT}",
        "stray name": f"the agent keychain has no token; {HINT}",
    }[fault]
    if fault == "unreachable":
        ready.fake.status = 503
    elif fault == "no item":
        del ready.fake.items["v1"]["i1"]
    elif fault == "no field":
        private_fields(ready)[0]["label"] = "other"
    else:
        keychain = ready.stubs.home() / "Library" / "Keychains"
        (keychain / f"{secrets.KEYCHAIN}-db").unlink()
    if fault == "stray name":
        # A read that fails after printing more than the name has no token.
        name = token_name_file(ready, "host-b")
        name.write_text(f"{name.read_text().strip()}\nstray\n")
    agent = private_config(ready, "agent")
    agent.write_text("older\n")
    assert run(ready, capfd, "upgrade") == 1
    assert gitleaks_lines(ready) == dict.fromkeys(
        ACCOUNTS, f"failed: {why}; wrote nothing"
    )
    assert agent.read_text() == "older\n"
    assert private_config(ready, "operator").read_text() == GITLEAKS
    assert_no_config_leaks(ready)


def test_a_copy_that_cannot_be_installed_fails_alone(
    ready: World, capfd: pytest.CaptureFixture[str]
) -> None:
    directory = private_config(ready, "agent").parent
    shutil.rmtree(directory)
    directory.write_text("not a directory\n")
    assert run(ready, capfd, "upgrade") == 1
    lines = gitleaks_lines(ready)
    assert lines["agent"].startswith(f"failed: cannot install {PRIVATE}: mkdir: ")
    assert lines["operator"] == CURRENT
    assert directory.read_text() == "not a directory\n"
    assert_no_config_leaks(ready)


# Generating the private gitleaks config. Every value below is a stand-in.

FACTS = host_tool.Facts(
    repos=(
        ("example-org", "fake-widget"),
        ("example-org", "gadgets"),
        ("example-org", "boxes"),
        ("example-org", "Widgets"),
        ("example-org", ".fake-dotrepo"),
        ("example-org", ".gadget"),
        ("example-org", "zqxw"),
        ("example-user", "zqx"),
    ),
    tailnet=host_tool.Tailnet(
        "example.ts.net",
        "Fake Tailnet (example)",
        ("box", "fakehost-01", "fakehost-02", "lone-node"),
    ),
    words=frozenset({"box", "gadget", "host", "operator", "widget"}),
    operators=("fakeop", "operator"),
    checkout=PurePosixPath("Fake/checkouts"),
    vaults=("vault-all", "host-fake", "box"),
    items=("gitleaks-private",),
    endpoints=("connect.fake.test",),
    names=("fake-agent.keychain", "fake-connect"),
)
# Each category's rule count for FACTS, and how many values it left out.
COUNTS = {
    "private-repo": (8, 5),
    "tailnet-suffix": (1, 0),
    "tailnet-name": (1, 0),
    "tailnet-host": (3, 1),
    "tailnet-stem": (1, 0),
    "tailnet-ipv4": (1, 0),
    "tailnet-ipv6": (1, 0),
    "home-path": (3, 0),
    "checkout-path": (1, 0),
    "tool-dir": (1, 0),
    "private-ipv4": (3, 0),
    "local-name": (1, 0),
    "mac-address": (1, 0),
    "op-reference": (1, 0),
    "session-socket": (1, 0),
    "account-name": (1, 1),
    "op-account": (1, 0),
    "op-vault": (2, 1),
    "op-item": (1, 0),
    "endpoint": (1, 0),
    "keychain": (2, 0),
}
CANARY = "tbhb-private-canary"
# Lines the config must flag, each with the categories of the rules that must
# match it, and lines it must leave alone.
CAUGHT = [
    ("see example-org/fake-widget for it", {"private-repo"}),
    ("the fake-widget readme", {"private-repo"}),
    ("clone EXAMPLE-ORG/gadgets", {"private-repo"}),
    ("example-org/boxes and example-org/widgets", {"private-repo"}),
    ("cd .fake-dotrepo/src", {"private-repo"}),
    ("example-org/.gadget", {"private-repo"}),
    ("zqxw is ready", {"private-repo"}),
    ("example-user/zqx", {"private-repo"}),
    ("ssh fakehost-01.example.ts.net", {"tailnet-suffix", "tailnet-host"}),
    ("lone-node is up", {"tailnet-host"}),
    ("fakehost-99 joins later", {"tailnet-stem"}),
    ("https://svc.example.ts.net/api", {"tailnet-suffix"}),
    ("the Fake Tailnet (example) tailnet", {"tailnet-name"}),
    ("reach 100.64.0.1 directly", {"tailnet-ipv4"}),
    ("reach fd7a:115c:a1e0::1 directly", {"tailnet-ipv6"}),
    ("open /Users/fakeop/notes", {"home-path"}),
    ("open /home/agent/work", {"home-path"}),
    ("open /Users/operator/work", {"home-path"}),
    ("cd /srv/Fake/checkouts/app", {"checkout-path"}),
    # Stand-in paths into .config, which gitleaks flags as credential paths.
    ("cat /data/.config/awrel/state", {"tool-dir"}),  # gitleaks:allow
    ("cat /data/.local/state/tbhbdev/log", {"tool-dir"}),
    ("ping 10.1.2.3", {"private-ipv4"}),
    ("ping 172.20.0.5", {"private-ipv4"}),
    ("ping 192.168.1.10", {"private-ipv4"}),
    ("ping printer.local", {"local-name"}),
    ("ping nas.lan", {"local-name"}),
    ("ether aa:bb:cc:dd:ee:ff", {"mac-address"}),
    ("ether AA-BB-CC-DD-EE-01", {"mac-address"}),
    ("read op://fake-vault/fake-item/field", {"op-reference"}),
    ("listen on /tmp/example-socks/fake.sock", {"session-socket"}),
    ("signed by fakeop", {"account-name"}),
    (f"sign in to {ACCOUNT}", {"op-account"}),
    ("the host-fake vault", {"op-vault"}),
    ("the gitleaks-private item", {"op-item"}),
    ("connect to connect.fake.test", {"endpoint"}),
    ("unlock fake-agent.keychain", {"keychain"}),
    ("the fake-connect server", {"keychain"}),
    ("tbhb-private-canary-0123abcd", {CANARY}),
    ("committed-stand-in-1234", {"committed-stand-in"}),
]
CLEAN = [
    "nothing private here",
    "some gadgets and widgets in a box of boxes",
    "a .gadget file",
    "run zqx now",
    "the operator said so",
    "fake-widgets are something else",
    "cd ~/Fake/checkouts/app",
    "cd $HOME/Fake/checkouts/app",
    # Stand-in paths into .config, which gitleaks flags as credential paths.
    "cat ${HOME}/.config/awrel/state",  # gitleaks:allow
    "cat ~/.config/gh-agent/hosts.yml",  # gitleaks:allow
    "cat $HOME/.cache/gitleaks/x",
    "local = threading.local()",
    "ping 198.51.100.7 and 203.0.113.9",
    "open /Users/fakeoperator/notes",
]


def category(rule_id: str) -> str:
    return (
        rule_id
        if rule_id in {CANARY, "committed-stand-in"}
        else rule_id.rsplit("-", 1)[0]
    )


def scan(tmp_path: Path, config: str, lines: list[str]) -> dict[int, set[str]]:
    """Run the real gitleaks over LINES: each flagged line's rule categories."""
    gitleaks = shutil.which("gitleaks")
    assert gitleaks, "gitleaks isn't on PATH"
    root = tmp_path / "root"
    root.mkdir()
    (root / ".gitleaks.toml").write_text(
        '[[rules]]\nid = "committed-stand-in"\n'
        "regex = '''committed-stand-in-[0-9]{4}'''\n"
    )
    private = tmp_path / "private.toml"
    private.write_text(config)
    report = tmp_path / "report.json"
    done = subprocess.run(  # nosec B603
        [
            gitleaks,
            "stdin",
            "--no-banner",
            "--redact",
            "--config",
            str(private),
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    assert done.returncode in {0, 1}, done.stderr
    found: dict[int, set[str]] = {}
    for finding in cast("list[Json]", json.loads(report.read_text())):
        line = cast("int", finding["StartLine"])
        found.setdefault(line, set()).add(category(str(finding["RuleID"])))
    return found


def generated(facts: host_tool.Facts = FACTS) -> str:
    rules, _ = host_tool.gitleaks_rules(facts)
    return host_tool.render_gitleaks(rules)


@pytest.mark.usefixtures("configured")
def test_the_generated_config_catches_each_category_and_passes_clean_lines(
    tmp_path: Path,
) -> None:
    lines = [line for line, _ in CAUGHT] + CLEAN
    found = scan(tmp_path, generated(), lines)
    for number, (line, wanted) in enumerate(CAUGHT, start=1):
        assert wanted <= found.get(number, set()), (line, found.get(number))
    for number, line in enumerate(CLEAN, start=len(CAUGHT) + 1):
        assert number not in found, (line, found[number])


@pytest.mark.usefixtures("configured")
def test_a_word_boundary_follows_the_first_and_last_characters() -> None:
    # Names whose first, second, second-to-last and last characters differ in
    # whether they are word characters, so each boundary comes from its own end.
    facts = dataclasses.replace(
        FACTS,
        repos=(("example-org", "a-fake-x"),),
        tailnet=host_tool.Tailnet("example.ts.net", "A-fake", ("a-boxcar", "a-boxing")),
        operators=("a-op-x",),
        checkout=PurePosixPath("a-fake/b-x"),
    )
    rules, _ = host_tool.gitleaks_rules(facts)
    wanted = {
        "private-repo",
        "tailnet-name",
        "tailnet-stem",
        "home-path",
        "checkout-path",
    }
    regexes = {(r.category, r.regex) for r in rules if r.category in wanted}
    assert regexes == {
        ("private-repo", r"(?i)(?:\bexample\-org/|\b)a\-fake\-x\b"),
        ("tailnet-name", r"(?i)\bA\-fake\b"),
        ("tailnet-stem", r"(?i)\ba\-box"),
        ("home-path", r"/(?:Users|home)/a\-op\-x\b"),
        ("home-path", r"/(?:Users|home)/agent\b"),
        ("checkout-path", r"""[^\s"'`<>()\[\],;=]*/a\-fake/b\-x\b"""),
    }


@pytest.mark.usefixtures("configured")
def test_the_generated_config_numbers_its_rules_by_category() -> None:
    text = generated()
    assert host_tool.gitleaks_problem(text) is None
    assert text.startswith(
        "# Generated by `tbhbdev host bootstrap`, which replaces any edit.\n\n"
        '[extend]\npath = ".gitleaks.toml"\n\n'
    )
    assert text.endswith('\\b"\n')
    data = tomllib.loads(text)
    rules = cast("list[Json]", data["rules"])
    assert rules[0] == {
        "id": CANARY,
        "description": "The canary: the private config is in use",
        "regex": host_tool.CANARY_REGEX,
    }
    wanted = [
        f"{name}-{n}"
        for name, (count, _) in COUNTS.items()
        for n in range(1, count + 1)
    ]
    assert [r["id"] for r in rules[1:]] == wanted
    for rule in rules[1:]:
        name = category(str(rule["id"]))
        assert rule["description"] == host_tool.GITLEAKS_RULES[name]
        allow = [{"regexes": [host_tool.GENERIC_HOME]}]
        assert rule.get("allowlists") == (
            allow if name in {"checkout-path", "tool-dir"} else None
        )


@pytest.mark.usefixtures("configured")
def test_the_rules_count_what_they_left_out() -> None:
    _, omitted = host_tool.gitleaks_rules(FACTS)
    assert omitted == {k: left for k, (_, left) in COUNTS.items() if k in omitted}
    assert set(omitted) == {
        "private-repo",
        "tailnet-host",
        "tailnet-stem",
        "account-name",
        "op-vault",
        "op-item",
        "endpoint",
        "keychain",
    }


@pytest.mark.usefixtures("configured")
def test_without_a_workspace_there_is_no_checkout_rule() -> None:
    rules, _ = host_tool.gitleaks_rules(dataclasses.replace(FACTS, checkout=None))
    assert "checkout-path" not in {r.category for r in rules}


@pytest.mark.usefixtures("configured")
def test_without_session_sockets_there_is_no_socket_rule() -> None:
    edit_config('session_sockets = ["/tmp/example-socks/"]\n', "")
    rules, _ = host_tool.gitleaks_rules(FACTS)
    assert "session-socket" not in {r.category for r in rules}


@pytest.mark.parametrize(
    ("hosts", "wanted"),
    [
        (("fakehost-01", "fakehost-02", "lone-node"), ["fakehost"]),
        (("abcd1", "abcd"), ["abcd"]),
        (("abcd-x", "abcd-y", "abce"), ["abcd"]),
        (("ab", "abc", "zz9-1", "zz9-2"), ["zz"]),
        (("solo",), []),
    ],
)
def test_stems_come_from_families_of_host_names(
    hosts: tuple[str, ...], wanted: list[str]
) -> None:
    assert host_tool.stems(hosts) == wanted


@pytest.mark.usefixtures("configured")
def test_a_short_stem_gets_no_rule() -> None:
    net = host_tool.Tailnet("example.ts.net", "Fake", ("zz9-1", "zz9-2"))
    _, omitted = host_tool.gitleaks_rules(dataclasses.replace(FACTS, tailnet=net))
    assert omitted["tailnet-stem"] == 1


def test_a_config_that_fails_its_check_is_never_published(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    status = {**TAILNET, "CurrentTailnet": {"Name": "Fake \U0001f600"}}
    world.stubs.set(tailscale=status)
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 1
    assert world.err == (
        f"{PROG}: the private gitleaks config: {REF} isn't valid TOML; wrote nothing\n"
    )
    assert "item" not in " ".join(world.op_calls())


# The sources.


def test_private_repos_lists_each_owner(world: World) -> None:
    assert host_tool.private_repos() == [
        ("example-org", "fake-widget"),
        ("example-org", "gadgets"),
        ("example-org", ".fake-dotrepo"),
        ("example-user", "zq"),
    ]
    assert [" ".join(argv) for argv in world.stubs.argvs("gh")] == [
        f"repo list {owner} --visibility private --limit 1000 --json name"
        for owner in ("example-org", "example-user")
    ]


def test_the_second_owner_may_have_no_private_repos(world: World) -> None:
    world.stubs.set(gh_repos={"example-org": ["fake-widget"]})
    assert host_tool.private_repos() == [("example-org", "fake-widget")]


@pytest.mark.parametrize(
    ("state", "error"),
    [
        (
            {"gh_fail": {"example-user": "auth needed"}},
            "gh repo list example-user failed: auth needed",
        ),
        ({"gh_repos": {}}, "gh repo list example-org listed no private repositories"),
        (
            {"gh_raw": {"example-org": "oops"}},
            "gh repo list example-org printed something other than JSON",
        ),
        (
            {"gh_raw": {"example-user": "{}"}},
            "gh repo list example-user printed something other than named repositories",
        ),
        (
            {"gh_raw": {"example-user": "[1]"}},
            "gh repo list example-user printed something other than named repositories",
        ),
        (
            {"gh_raw": {"example-user": '[{"name": ""}]'}},
            "gh repo list example-user printed something other than named repositories",
        ),
    ],
)
def test_private_repos_fails_closed(world: World, state: Json, error: str) -> None:
    world.stubs.set(**state)
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.private_repos()
    assert str(caught.value) == error


@pytest.mark.usefixtures("world")
def test_a_repo_list_at_its_limit_may_be_cut_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_tool, "REPO_LIMIT", 3)
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.private_repos()
    assert str(caught.value) == (
        "gh repo list example-org reached its limit of 3 and may miss some"
    )
    monkeypatch.setattr(host_tool, "REPO_LIMIT", 4)
    assert len(host_tool.private_repos()) == 4


def test_tailnet_facts_reads_the_suffix_name_and_dns_names(world: World) -> None:
    assert host_tool.tailnet_facts() == host_tool.Tailnet(
        "example.ts.net", "Fake Tailnet (example)", ("host-a", "host-b")
    )
    assert world.stubs.argvs("tailscale") == [["status", "--json"]]


def test_tailnet_facts_takes_the_binary_to_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = world.stubs.dir / "Tailscale"
    shutil.move(world.stubs.bin / "tailscale", app)
    # A stubs-only PATH, so a real tailscale elsewhere on PATH never runs.
    monkeypatch.setenv("PATH", str(world.stubs.bin))
    monkeypatch.setattr(host_tool, "TAILSCALE_APP", str(world.stubs.dir / "none"))
    assert host_tool.tailnet_facts(str(app)).suffix == "example.ts.net"
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.tailnet_facts()
    assert str(caught.value) == (
        f"tailscale isn't on PATH and {world.stubs.dir / 'none'} doesn't exist"
    )


def test_tailnet_facts_falls_back_to_the_app_bundle(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = world.stubs.dir / "Tailscale"
    shutil.move(world.stubs.bin / "tailscale", app)
    monkeypatch.setenv("PATH", str(world.stubs.bin))
    monkeypatch.setattr(host_tool, "TAILSCALE_APP", str(app))
    assert host_tool.tailnet_facts().hosts == ("host-a", "host-b")


def test_tailnet_facts_ignores_a_directory_at_the_app_path(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    (world.stubs.bin / "tailscale").unlink()
    monkeypatch.setenv("PATH", str(world.stubs.bin))
    monkeypatch.setattr(host_tool, "TAILSCALE_APP", str(world.stubs.dir))
    with pytest.raises(host_tool.HostError, match="isn't on PATH"):
        host_tool.tailnet_facts()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("not json", "tailscale status printed something other than JSON"),
        ([], "tailscale status printed something other than an object"),
        ({**TAILNET, "MagicDNSSuffix": ""}, "tailscale status has no MagicDNSSuffix"),
        (
            {**TAILNET, "CurrentTailnet": None},
            "tailscale status has no CurrentTailnet.Name",
        ),
        (
            {**TAILNET, "CurrentTailnet": {}},
            "tailscale status has no CurrentTailnet.Name",
        ),
        ({**TAILNET, "Self": {}, "Peer": None}, "tailscale status has no DNSName"),
    ],
)
def test_tailnet_facts_fails_closed(world: World, status: object, error: str) -> None:
    world.stubs.set(tailscale=status)
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.tailnet_facts()
    assert str(caught.value) == error


def test_tailnet_facts_names_a_tailscale_failure(world: World) -> None:
    world.stubs.set(tailscale_error="stopped")
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.tailnet_facts()
    assert str(caught.value) == "tailscale status failed: tailscale: stopped"


def test_tailnet_facts_lowercases_dns_names(world: World) -> None:
    world.stubs.set(
        tailscale={
            **TAILNET,
            "Self": {"DNSName": "Fake-Mac.example.ts.net."},
            "Peer": {},
        }
    )
    assert host_tool.tailnet_facts().hosts == ("fake-mac",)


@pytest.mark.usefixtures("world")
def test_common_words_are_the_dictionary_lowercased_and_tech_terms() -> None:
    words = host_tool.common_words()
    assert {"host", "gadget", "github", "config"} <= words
    assert "Host" not in words


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize("content", [None, b"\xff\xfe", b"\n"])
def test_common_words_fails_closed(content: bytes | None) -> None:
    words = host_tool.WORDS
    if content is None:
        words.unlink()
    else:
        words.write_bytes(content)
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.common_words()
    wanted = f"{words} lists no words" if content == b"\n" else f"cannot read {words}"
    assert str(caught.value) == wanted


def test_endpoint_hosts_are_the_urls_host_names() -> None:
    assert host_tool.endpoint_hosts([*reversed(ENDPOINTS), ENDPOINTS[0]]) == [
        f"nas-a.{SUFFIX}",
        f"nas-b.{SUFFIX}",
    ]


def test_gitleaks_facts_gathers_every_source(world: World) -> None:
    world.stubs.set(
        op={
            "servers": [],
            "grants": [],
            "tokens": [],
            "vaults": [{"id": "v1", "name": "vault-all"}, {"id": "v2", "name": ""}],
        }
    )
    ws = host_tool.Workspace(PurePosixPath("Fake/checkouts"), ())
    facts = host_tool.gitleaks_facts(["fakeop", "fakeop"], ws, ENDPOINTS)
    assert facts == host_tool.Facts(
        repos=(
            ("example-org", "fake-widget"),
            ("example-org", "gadgets"),
            ("example-org", ".fake-dotrepo"),
            ("example-user", "zq"),
        ),
        tailnet=host_tool.tailnet_facts(),
        words=host_tool.common_words(),
        operators=("fakeop",),
        checkout=PurePosixPath("Fake/checkouts"),
        vaults=("vault-all",),
        items=("gitleaks-private",),
        endpoints=(f"nas-a.{SUFFIX}", f"nas-b.{SUFFIX}"),
        names=("example-connect", secrets.SERVICE, secrets.KEYCHAIN),
    )
    assert host_tool.gitleaks_facts([], None, ENDPOINTS).checkout is None


@pytest.mark.usefixtures("world")
def test_gitleaks_facts_needs_a_vault() -> None:
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.gitleaks_facts(["fakeop"], None, ENDPOINTS)
    assert str(caught.value) == "op vault list listed no vaults"


# Publishing it.

CONFIG = "# stand-in config\n"


def items(world: World) -> list[Json]:
    vaults = cast("Json", world.account().get("items", {}))
    return cast("list[Json]", vaults.get("vault-all", []))


def item_calls(world: World) -> list[Json]:
    return [
        c
        for c in world.stubs.calls()
        if c["tool"] == "op" and cast("list[str]", c["argv"])[0] == "item"
    ]


def test_publish_creates_the_item_from_stdin(world: World) -> None:
    result = host_tool.publish_gitleaks(CONFIG)
    assert result == host_tool.Result("changed", f"created {REF}")
    assert items(world) == [
        {
            "title": "gitleaks-private",
            "category": "SECURE_NOTE",
            "fields": [
                {
                    "id": "config",
                    "label": "config",
                    "type": "CONCEALED",
                    "value": CONFIG,
                }
            ],
            "id": "itm-1",
        }
    ]
    calls = item_calls(world)
    assert [c["argv"] for c in calls][1] == [
        "item",
        "create",
        "-",
        "--vault",
        "vault-all",
        "--account",
        ACCOUNT,
    ]
    item = cast("Json", json.loads(str(calls[1]["stdin"])))
    assert cast("list[Json]", item["fields"])[0]["value"] == CONFIG
    assert all(CONFIG not in json.dumps(c["argv"]) for c in calls)


def test_publish_leaves_a_current_item_alone(world: World) -> None:
    host_tool.publish_gitleaks(CONFIG)
    before = len(item_calls(world))
    assert host_tool.publish_gitleaks(CONFIG) == host_tool.Result(
        "ok", f"{REF} is current"
    )
    argvs = [cast("list[str]", c["argv"]) for c in item_calls(world)[before:]]
    assert [argv[:2] for argv in argvs] == [["item", "list"], ["item", "get"]]
    assert argvs[1][2:-2] == [
        "gitleaks-private",
        "--vault",
        "vault-all",
        "--reveal",
        "--format",
        "json",
    ]


def test_publish_updates_only_the_config_field(world: World) -> None:
    host_tool.publish_gitleaks(CONFIG)
    notes: Json = {"id": "notesPlain", "label": "notesPlain", "value": "kept"}
    stale: Json = {"id": "config2", "label": "config", "value": "stale"}
    account = world.account()
    item = cast("list[Json]", cast("Json", account["items"])["vault-all"])[0]
    cast("list[Json]", item["fields"]).extend([notes, stale])
    world.stubs.set(op=account)
    assert host_tool.publish_gitleaks("# newer\n") == host_tool.Result(
        "changed", f"updated {REF}"
    )
    field = {
        "id": "config",
        "label": "config",
        "type": "CONCEALED",
        "value": "# newer\n",
    }
    assert items(world)[0]["fields"] == [notes, field]
    assert items(world)[0]["id"] == "itm-1"
    edit = item_calls(world)[-1]
    assert edit["argv"] == [
        "item",
        "edit",
        "gitleaks-private",
        "--vault",
        "vault-all",
        "--account",
        ACCOUNT,
    ]
    assert "# newer" not in json.dumps(edit["argv"])


def test_publish_refuses_two_items(world: World) -> None:
    host_tool.publish_gitleaks(CONFIG)
    account = world.account()
    cast("list[Json]", cast("Json", account["items"])["vault-all"]).append(
        {"id": "itm-2", "title": "gitleaks-private"}
    )
    world.stubs.set(op=account)
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.publish_gitleaks("# newer\n")
    assert str(caught.value) == "vault-all holds 2 items named gitleaks-private"


@pytest.mark.parametrize("command", ["item create", "item edit"])
def test_a_failed_write_names_only_the_exit_status(world: World, command: str) -> None:
    if command == "item edit":
        host_tool.publish_gitleaks(CONFIG)
    world.stubs.set(op_fail={command: f"bad input: {CONFIG}"})
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.publish_gitleaks("# newer\n")
    verb = "create -" if command == "item create" else "edit gitleaks-private"
    assert str(caught.value) == (f"op item {verb} --vault vault-all failed: exit 1")


@pytest.mark.parametrize(
    ("raw", "printed"), [("[]", "an object"), ("not json", "JSON")]
)
def test_publish_needs_an_object_from_item_get(
    world: World, raw: str, printed: str
) -> None:
    host_tool.publish_gitleaks(CONFIG)
    world.stubs.set(op_raw={"item get": raw})
    with pytest.raises(host_tool.HostError) as caught:
        host_tool.publish_gitleaks(CONFIG)
    assert str(caught.value) == (
        f"op item get gitleaks-private printed something other than {printed}"
    )


# Bootstrap generates it.

STAND_INS = ("fake-widget", "gadgets", "fake-dotrepo", "Fake Tailnet", "example.ts.net")


def published(world: World) -> str:
    fields = cast("list[Json]", items(world)[0]["fields"])
    return str(fields[0]["value"])


def test_bootstrap_publishes_the_generated_config(
    world: World, config_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    with_workspace(config_file, "Fake/checkouts")
    world.stubs.set(operators={"host-a": "fakeop"})
    assert run(world, capfd, "bootstrap", "--all") == 0
    lines = [
        line.removeprefix("gitleaks-generate: ")
        for line in world.out.splitlines()
        if line.startswith("gitleaks-generate: ")
    ]
    assert lines == [
        "private-repo: 4 rule(s), 2 too common or short to match alone",
        "tailnet-suffix: 1 rule(s)",
        "tailnet-name: 1 rule(s)",
        "tailnet-host: 2 rule(s)",
        "tailnet-stem: 0 rule(s), 1 too common or short to match alone",
        "tailnet-ipv4: 1 rule(s)",
        "tailnet-ipv6: 1 rule(s)",
        "home-path: 3 rule(s)",
        "checkout-path: 1 rule(s)",
        "tool-dir: 1 rule(s)",
        "private-ipv4: 3 rule(s)",
        "local-name: 1 rule(s)",
        "mac-address: 1 rule(s)",
        "op-reference: 1 rule(s)",
        "session-socket: 1 rule(s)",
        "account-name: 1 rule(s), 1 too common or short to match alone",
        "op-account: 1 rule(s)",
        "op-vault: 4 rule(s)",
        "op-item: 1 rule(s)",
        "endpoint: 2 rule(s)",
        "keychain: 3 rule(s)",
        f"changed: created {REF}",
    ]
    # Generation comes before the upgrade, which reads the config.
    assert world.out.index("gitleaks-generate: changed") < world.out.index(
        "host-a: token: "
    )
    text = published(world)
    assert host_tool.gitleaks_problem(text) is None
    assert "fake\\\\-widget" in text
    for call in world.stubs.calls():
        if call["tool"] != "tailscale":
            assert not any(v in json.dumps(call["argv"]) for v in STAND_INS), call
    for stream in (world.out, world.err):
        assert not any(v in stream for v in STAND_INS)


def test_a_failed_source_stops_bootstrap_before_it_writes(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(gh_fail={"example-org": "HTTP 401"})
    assert run(world, capfd, "bootstrap", "--all") == 1
    assert world.err == (
        f"{PROG}: the private gitleaks config: gh repo list example-org failed: "
        "HTTP 401; wrote nothing\n"
    )
    assert "item" not in " ".join(world.op_calls())
    assert ": token: " not in world.out


def test_a_single_host_bootstrap_keeps_the_other_hosts_operators(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(operators={"host-a": "fakeop", "host-c": "fakeoperator"})
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert "gitleaks-generate: account-name: 2 rule(s), 1 too common" in world.out
    assert "fakeoperator" in published(world)


def test_bootstrap_keeps_the_published_config_without_every_operator(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(id_fails=["host-c"])
    assert run(world, capfd, "bootstrap", "--host", "host-a") == 0
    assert "host-c: cannot find out who you are there" in world.err
    assert "keeping the published private gitleaks config\n" in world.out
    assert "gitleaks-generate" not in world.out
    assert not items(world)


def test_bootstrap_with_no_host_ready_keeps_the_published_config(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    world.stubs.set(id_fails=True)
    assert run(world, capfd, "bootstrap", "--all") == 1
    assert "gitleaks-generate" not in world.out
    assert "item" not in " ".join(world.op_calls())
