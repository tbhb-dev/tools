"""Fakes for the tests of plugins, secrets and agent hosts.

`installed` hides every installed tbhbdev plugin from each test and registers
stub plugins in their place. `config_file` is the user config in a temporary
XDG_CONFIG_HOME, which no test has until it writes one, and `config` writes
one of example values there. `connect` starts in-memory 1Password Connect APIs
on 127.0.0.1, and `stubs` makes PATH hold only stub op, ssh, sudo, security,
tailscale, dscl, chmod, git and friends (see stubtool.py), and
the system tools the host scripts use, with HOME in tmp_path. `plain` turns
argparse color off for the tests that compare help and usage exactly. No test
reaches 1Password, a keychain, the tailnet, a host, an account or an ACL.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import pytest

from tbhbdev import host, plugins, secrets

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tbhbdev.cli import Tool

type Json = dict[str, object]

STUBTOOL = Path(__file__).resolve().parent / "stubtool.py"
STUB_TOOLS = (
    "chmod",
    "dscl",
    "getent",
    "getfacl",
    "gh",
    "git",
    "groupadd",
    "id",
    "install",
    "ls",
    "mise",
    "op",
    "security",
    "setfacl",
    "ssh",
    "sudo",
    "swift",
    "tailscale",
    "useradd",
    "usermod",
)
# The system tools the host scripts, the stubs and the exec tests run, linked
# beside the stubs so PATH holds nothing else, and no real tailscale, op or
# ssh can run. The chmod stub runs the real chmod for a plain mode change.
SYSTEM_TOOLS = (
    "cat",
    "cmp",
    "echo",
    "find",
    "mkdir",
    "mktemp",
    "mv",
    "rm",
    "sh",
    "true",
)
# The fake Connect API's own token, which reaches every vault.
TOKEN = "fake-test-token"  # noqa: S105


@pytest.fixture(autouse=True)
def no_operator_credentials(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare op calls fail without reaching the desktop app or an SSH agent.

    The stub's directory is outside tmp_path, which tests list; the agent
    socket path under tmp_path is never created.
    """
    directory = tmp_path_factory.mktemp("no-credentials")
    stub = directory / "op"
    stub.write_text(
        '#!/bin/sh\necho "tests: real 1Password access is disabled" >&2\nexit 1\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "no-ssh-agent"))


@dataclass
class FakeConnect:
    """A Connect API: vaults, items, the tokens that reach them, and faults.

    `tokens` maps a token to the vault names it reaches, or None for every
    vault. `tokens_file` adds the tokens the op stub has created. `status`
    makes every request answer with that HTTP status. `fail` makes a method,
    or a "METHOD /path", answer with a status instead of doing its work, and
    `fail_after` makes a method answer with one after doing it, and `raw`
    answers every request with that body.
    """

    vaults: dict[str, str] = field(default_factory=dict)
    items: dict[str, dict[str, Json]] = field(default_factory=dict)
    tokens: dict[str, list[str] | None] = field(default_factory=lambda: {TOKEN: None})
    tokens_file: Path | None = None
    status: int | None = None
    fail: dict[str, int] = field(default_factory=dict)
    fail_after: dict[str, int] = field(default_factory=dict)
    raw: bytes | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)
    url: str = ""
    count: int = 0
    token: str = TOKEN

    def vault(self, name: str) -> str:
        vault_id = f"v{len(self.vaults) + 1}"
        self.vaults[vault_id] = name
        self.items[vault_id] = {}
        return vault_id

    def item(self, vault_id: str, title: str, **values: str) -> str:
        self.count += 1
        item_id = f"i{self.count}"
        self.items[vault_id][item_id] = {
            "id": item_id,
            "title": title,
            "category": "LOGIN",
            "vault": {"id": vault_id},
            "version": 1,
            "fields": [
                {"id": f"f{n}", "label": k, "type": "CONCEALED", "value": v}
                for n, (k, v) in enumerate(values.items())
            ],
        }
        return item_id

    def reach(self, auth: str) -> list[str]:
        tokens = dict(self.tokens)
        if self.tokens_file is not None and self.tokens_file.exists():
            tokens.update(json.loads(self.tokens_file.read_text()))
        token = auth.removeprefix("Bearer ")
        if token not in tokens:
            raise KeyError(token)
        names = tokens[token]
        if names is None:
            return list(self.vaults)
        return [i for i, n in self.vaults.items() if n in names]

    def handle(self, method: str, path: str, auth: str, body: Json | None) -> object:
        """The answer to one request: a JSON value, or an int status."""
        self.requests.append((method, path))
        if self.status is not None:
            return self.status
        try:
            reach = self.reach(auth)
        except KeyError:
            return 401
        parts = path.strip("/").split("/")[1:]
        if parts == ["vaults"]:
            return [{"id": v, "name": self.vaults[v]} for v in reach]
        if parts[1] not in reach:
            return 403
        before = self.fail.get(f"{method} {path}", self.fail.get(method))
        if before is not None:
            return before
        answer = self.items_route(method, parts[1], parts[3:], body)
        return self.fail_after.get(method, answer)

    def items_route(
        self, method: str, vault_id: str, rest: list[str], body: Json | None
    ) -> object:
        items = self.items[vault_id]
        if not rest:
            if method == "POST":
                self.count += 1
                item = dict(body or {})
                item["id"] = f"i{self.count}"
                items[item["id"]] = item
                return item
            return [
                {k: v for k, v in item.items() if k != "fields"}
                for item in items.values()
            ]
        if rest[0] not in items:
            return 404
        if method == "DELETE":
            del items[rest[0]]
            return None
        if method == "PUT":
            items[rest[0]] = dict(body or {})
        return items[rest[0]]


def _handler(fake: FakeConnect) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = cast("Json", json.loads(raw)) if raw else None
            auth = self.headers.get("Authorization") or ""
            if fake.raw is not None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(fake.raw)))
                self.end_headers()
                self.wfile.write(fake.raw)
                return
            answer = fake.handle(self.command, self.path, auth, body)
            status = answer if isinstance(answer, int) else 200
            data = b"" if answer is None else json.dumps(answer).encode()
            if isinstance(answer, int):
                data = json.dumps({"status": answer, "message": "fake"}).encode()
            self.send_response(204 if answer is None else status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # http.server calls these by name, which vulture can't see.
        do_GET = do_POST = do_PUT = do_DELETE = _serve  # noqa: N815, V107

        @override  # noqa: V105
        def log_message(self, format: str, *args: object) -> None:  # noqa: V107
            """Keep the request log out of the tests' captured stderr."""

    return Handler


@pytest.fixture
def connect() -> Iterator[Callable[[], FakeConnect]]:
    """Start fake Connect APIs; each call starts one more."""
    servers: list[ThreadingHTTPServer] = []

    def start() -> FakeConnect:
        fake = FakeConnect()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
        fake.url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
        servers.append(server)
        return fake

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Help and usage at 80 columns, without color, even under -E or FORCE_COLOR."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("COLUMNS", "80")


@dataclass
class StubPlugin:
    """A plugin object: extra tools, and config files by name."""

    TOOLS: dict[str, Tool] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)

    def config(self, name: str) -> str | None:
        return self.files.get(name)


@dataclass
class StubEntryPoint:
    """An entry point that loads TARGET, or raises it when it's an exception."""

    name: str
    target: object

    def load(self) -> object:
        if isinstance(self.target, BaseException):
            raise self.target
        return self.target


@dataclass
class Installed:
    """The entry points the fake `entry_points` returns, in order."""

    points: list[object] = field(default_factory=list)

    def add(self, name: str, target: object) -> None:
        self.points.append(StubEntryPoint(name, target))
        plugins.load.cache_clear()

    def stub(
        self,
        name: str,
        *,
        tools: dict[str, Tool] | None = None,
        files: dict[str, str] | None = None,
    ) -> None:
        """Add a plugin with TOOLS and the config FILES, by name."""
        self.add(name, StubPlugin(tools or {}, files or {}))


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Installed]:
    """No installed plugin, only the stubs a test adds."""
    found = Installed()

    def entry_points(*, group: str) -> list[object]:
        return found.points if group == plugins.GROUP else []

    monkeypatch.setattr(plugins, "entry_points", entry_points)
    plugins.load.cache_clear()
    yield found
    plugins.load.cache_clear()


def example_config(urls: list[str] | None = None) -> str:
    """A complete user config of example values that lists the endpoints URLS.

    Without URLS it has no endpoints, as the operator's config doesn't.
    """
    lines = ",\n".join(f'  "{url}"' for url in urls or [])
    listed = f"endpoints = [\n{lines},\n]\n" if urls else ""
    head = (
        '[op]\naccount = "example.1password.com"\n'
        '[connect]\nserver = "example-connect"\n'
    )
    return (
        head
        + listed
        + (
            'server_hosts = ["nas-a", "nas-b"]\n'
            'token_prefix = "example-host"\n'
            '[agent]\nhost_tag = "tag:example-agents"\n'
            '[vaults]\nall = "vault-all"\nhost_prefix = "vault-"\n'
            '[gitleaks]\nowners = ["example-org", "example-user"]\n'
            'session_sockets = ["/tmp/example-socks/"]\n'
        )
    )


@pytest.fixture(autouse=True)
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user config's path, in a temporary XDG_CONFIG_HOME, not yet written."""
    xdg = tmp_path / "xdg"
    (xdg / "tbhbdev").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg / "tbhbdev" / "config.toml"


@pytest.fixture
def config(config_file: Path) -> Callable[[list[str] | None], None]:
    """Write a user config of example values that lists the endpoints URLS."""

    def supply(urls: list[str] | None) -> None:
        config_file.write_text(example_config(urls))

    return supply


@dataclass
class Stubs:
    """The stub executables' directory, scenario state and call log."""

    dir: Path
    bin: Path

    def state(self) -> Json:
        path = self.dir / "state.json"
        return cast("Json", json.loads(path.read_text())) if path.exists() else {}

    def set(self, **values: object) -> None:
        state = self.state()
        state.update(values)
        (self.dir / "state.json").write_text(json.dumps(state))

    def calls(self, tool: str | None = None) -> list[Json]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        entries = [
            cast("Json", json.loads(line)) for line in path.read_text().splitlines()
        ]
        return [e for e in entries if tool is None or e["tool"] == tool]

    def argvs(self, tool: str) -> list[list[str]]:
        return [cast("list[str]", e["argv"]) for e in self.calls(tool)]

    def home(self, host: str = "local") -> Path:
        return self.dir / "homes" / host / "agent"


def write_stub(launcher: Path, tool: str) -> None:
    """Write an executable that runs stubtool.main(tool, argv) under -I.

    The launcher is a /bin/sh script that execs the interpreter, because
    Linux refuses a script whose first line passes 256 bytes, and a
    `#!{sys.executable}` line does in a deep sandbox.
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(STUBTOOL.parent)!r})\n"
        "import stubtool\n"
        f"stubtool.main({tool!r}, sys.argv[1:])\n"
    )
    launcher.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} -I -c {shlex.quote(code)} "$@"\n'
    )
    launcher.chmod(0o755)


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stubs:
    """Stub tools alone on PATH, HOME in tmp_path, and no token override."""
    stub_dir = tmp_path / "stub"
    bin_dir = stub_dir / "bin"
    bin_dir.mkdir(parents=True)
    for tool in STUB_TOOLS:
        write_stub(bin_dir / tool, tool)
    for tool in SYSTEM_TOOLS:
        found = shutil.which(tool, path=f"/usr/bin{os.pathsep}/bin")
        assert found is not None, tool
        (bin_dir / tool).symlink_to(found)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    # Without a tailscale on PATH, host falls back to the app bundle.
    monkeypatch.setattr(host, "TAILSCALE_APP", str(stub_dir / "no-tailscale-app"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("STUB_DIR", str(stub_dir))
    monkeypatch.delenv(secrets.TEST_TOKEN_ENV, raising=False)
    return Stubs(stub_dir, bin_dir)
