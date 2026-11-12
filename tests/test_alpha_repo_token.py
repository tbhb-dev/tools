"""Explicit credentials, fake HTTP transports, and real offline Git probes."""

from __future__ import annotations

# ruff: noqa: SLF001  # Exercise the private helper and subprocess input seam.
import base64
import dataclasses
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, override

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tbhbdev import secrets
from tbhbdev.alpha import gh_app
from tbhbdev.alpha import repo_token as rt

if TYPE_CHECKING:
    from collections.abc import Callable

TOKEN = "test-repo-token"  # noqa: S105  # Synthetic fixture.
CONNECT_TOKEN = "test-connect-token"  # noqa: S105  # Synthetic fixture.
REPO = "example/widget"
ITEM = "op://vault/app"
URLS = ["https://connect-a.example", "https://connect-b.example"]
PERMISSIONS = {"contents": "read"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_self: httpx.HTTPTransport, _request: httpx.Request) -> NoReturn:
        pytest.fail("HTTP must use a fake transport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


class NoEnvironment(dict[str, str]):
    """Reject ambient inputs while allowing mutmut's instrumentation marker."""

    @override
    def __contains__(self, key: object) -> bool:
        assert key == "MUTANT_UNDER_TEST", "environment accessed"
        return super().__contains__(key)

    @override
    def __getitem__(self, key: str) -> str:
        assert key == "MUTANT_UNDER_TEST", "environment accessed"
        return super().__getitem__(key)

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        assert key == "MUTMUT_DEPENDENCY_DEPTH", "environment accessed"
        return default


def no_environment() -> NoEnvironment:
    marker = os.environ.get("MUTANT_UNDER_TEST")
    return NoEnvironment({} if marker is None else {"MUTANT_UNDER_TEST": marker})


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "connect-token"
    path.write_text(CONNECT_TOKEN + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def app() -> rt.AppKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return rt.AppKey("123", pem)


def assert_kind(kind: rt.Kind, call: Callable[[], object]) -> None:
    with pytest.raises(rt.RepoTokenError) as caught:
        call()
    assert caught.value.kind == kind
    assert str(caught.value) == rt.MESSAGES[kind]
    assert TOKEN not in str(caught.value)
    assert CONNECT_TOKEN not in repr(caught.value)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


@pytest.mark.parametrize(
    "problem",
    [
        "symlink",
        "directory",
        "fifo",
        "0640",
        "0644",
        "0400",
        "owner",
        "parent",
        "parent_owner",
        "missing",
        "unicode",
        "empty",
    ],
)
def test_token_file_rejections(
    token_file: Path,
    problem: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if problem == "symlink":
        target = token_file.with_name("target")
        token_file.rename(target)
        token_file.symlink_to(target)
    elif problem in {"directory", "fifo", "missing"}:
        token_file.rename(token_file.with_name("original"))
        if problem == "directory":
            token_file.mkdir()
        elif problem == "fifo":
            os.mkfifo(token_file, 0o600)
    elif problem.startswith("0"):
        token_file.chmod(int(problem, 8))
    elif problem == "owner":
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    elif problem == "parent":
        token_file.parent.chmod(0o770)
    elif problem == "parent_owner":
        real = os.fstat

        def found(fd: int) -> os.stat_result:
            value = real(fd)
            entries = list(value)
            if stat.S_ISDIR(value.st_mode):
                entries[4] = os.getuid() + 1
            return os.stat_result(entries)

        monkeypatch.setattr(os, "fstat", found)
    elif problem == "unicode":
        token_file.write_bytes(b"\xff")
    elif problem == "empty":
        token_file.write_text(" \n")
    assert_kind("token_file", lambda: rt.read_app(token_file, ITEM, endpoints=URLS))


@pytest.fixture
def connect_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {CONNECT_TOKEN}"
        if request.url.host == "connect-a.example":
            return httpx.Response(503)
        if request.url.path == "/v1/vaults":
            return httpx.Response(200, json=[{"id": "v1", "name": "vault"}])
        if request.url.path == "/v1/vaults/v1/items":
            return httpx.Response(200, json=[{"id": "i1", "title": "app"}])
        assert request.url.path == "/v1/vaults/v1/items/i1"
        return httpx.Response(
            200,
            json={
                "fields": [
                    {"label": "app_id", "value": "123"},
                    {"label": "private_key", "value": "test-signing-key"},
                ]
            },
        )

    real = httpx.Client

    def client(
        *,
        base_url: str,
        headers: dict[str, str],
        timeout: httpx.Timeout,
        trust_env: bool,
    ) -> httpx.Client:
        assert trust_env is False
        assert timeout == secrets.TIMEOUT
        return real(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            trust_env=trust_env,
            transport=httpx.MockTransport(respond),
        )

    monkeypatch.setattr(httpx, "Client", client)
    return requests


def test_explicit_connect_never_uses_user_state(
    token_file: Path,
    connect_transport: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("implicit credentials or configuration accessed")

    ambient = no_environment()
    with monkeypatch.context() as patch:
        patch.setattr(secrets, "token", forbidden)
        patch.setattr(secrets, "endpoints", forbidden)
        patch.setattr(secrets.userconfig, "strings", forbidden)
        patch.setattr(Path, "home", forbidden)
        patch.setattr(os, "environ", ambient)
        app = rt.read_app(token_file, ITEM, endpoints=URLS)
    assert app == rt.AppKey("123", "test-signing-key")
    assert repr(app) == "AppKey()"
    assert [r.url.host for r in connect_transport] == ["connect-a.example"] + [
        "connect-b.example"
    ] * 3


@pytest.mark.parametrize("endpoints", [[], [URLS[0]]])
def test_all_connect_endpoints_fail(
    token_file: Path,
    connect_transport: list[httpx.Request],
    endpoints: list[str],
) -> None:
    assert_kind("connect", lambda: rt.read_app(token_file, ITEM, endpoints=endpoints))
    assert len(connect_transport) == len(endpoints)


@pytest.mark.parametrize(
    "item",
    [
        "vault/app",
        "op://vault",
        "op:///app",
        "op://vault/missing",
        "op://missing/app",
        "op://vault/app/field",
    ],
)
def test_missing_item(
    token_file: Path,
    connect_transport: list[httpx.Request],
    item: str,
) -> None:
    assert_kind("item", lambda: rt.read_app(token_file, item, endpoints=URLS))
    assert all(
        r.url.host in {"connect-a.example", "connect-b.example"}
        for r in connect_transport
    )


@pytest.mark.parametrize(
    "fields",
    [
        [],
        [{"label": "app_id", "value": "123"}],
        [{"label": "private_key", "value": "key"}],
        [{"label": "app_id", "value": " "}, {"label": "private_key", "value": "key"}],
    ],
)
def test_missing_fields(
    token_file: Path,
    fields: list[secrets.Json],
    connect_transport: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get(_self: secrets.Connect, _vault: str, _item: str) -> secrets.Json:
        return {"fields": fields}

    monkeypatch.setattr(secrets.Connect, "item", get)
    assert_kind("item", lambda: rt.read_app(token_file, ITEM, endpoints=URLS))
    assert connect_transport


@pytest.mark.parametrize("status", [404, 403])
def test_explicit_protocol_errors(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    def send(*_args: object) -> tuple[httpx.Response, bool]:
        return httpx.Response(status), False

    monkeypatch.setattr(secrets.Connect, "_send", send)
    error = (
        secrets.ExplicitItemMissingError
        if status == 404
        else secrets.ConnectUnavailableError
    )
    with pytest.raises(error):
        secrets.explicit_item(CONNECT_TOKEN, "vault", "app", urls=URLS)


@pytest.fixture
def github(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[httpx.Request], dict[str, httpx.Response]]:
    requests: list[httpx.Request] = []
    routes = {
        f"/repos/{REPO}/installation": httpx.Response(200, json={"id": 7}),
        "/app/installations/7/access_tokens": httpx.Response(
            201, json={"token": TOKEN, "expires_at": "2030-01-01T00:00:00Z"}
        ),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        jwt = request.headers["Authorization"].removeprefix("Bearer ")
        claims = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
        assert claims == {"iss": "123", "iat": 940, "exp": 1540}
        if request.method == "POST":
            assert json.loads(request.content) == {
                "repositories": ["widget"],
                "permissions": PERMISSIONS,
            }
        return routes.get(
            request.url.path, httpx.Response(404, json={"message": TOKEN})
        )

    monkeypatch.setattr(
        rt,
        "_MINT_ENV",
        dataclasses.replace(
            rt._MINT_ENV, now=lambda: 1000, transport=httpx.MockTransport(respond)
        ),
    )
    return requests, routes


@pytest.mark.parametrize("cached", [False, True])
def test_mint_cache(
    app: rt.AppKey,
    tmp_path: Path,
    github: tuple[list[httpx.Request], dict[str, httpx.Response]],
    cached: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = github
    cache = tmp_path / "installations.json"
    if cached:
        cache.write_text(json.dumps({REPO: 7, "example/other": 8}))
        cache.chmod(0o600)
    before = set(tmp_path.iterdir())
    replace = os.replace
    calls: list[str] = []

    def atomic(src: str, dst: Path) -> None:
        assert Path(src).parent == dst.parent
        assert stat.S_IMODE(Path(src).stat().st_mode) == 0o600
        assert json.loads(Path(src).read_text())[REPO] == 7
        calls.append("replace")
        replace(src, dst)

    sync = os.fsync

    def synced(fd: int) -> None:
        calls.append("fsync")
        sync(fd)

    monkeypatch.setattr(os, "replace", atomic)
    monkeypatch.setattr(os, "fsync", synced)
    minted = rt.mint(app, REPO, PERMISSIONS, installation_cache=cache)
    assert minted == rt.Minted(TOKEN, 1893456000)
    assert TOKEN not in repr(minted)
    assert calls == ["fsync", "replace"]
    assert [r.url.path for r in requests] == (
        [] if cached else [f"/repos/{REPO}/installation"]
    ) + ["/app/installations/7/access_tokens"]
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert json.loads(cache.read_text()) == (
        {REPO: 7, "example/other": 8} if cached else {REPO: 7}
    )
    assert set(tmp_path.iterdir()) == before | {cache}


def test_stale_installation_is_dropped_before_retry(
    app: rt.AppKey,
    tmp_path: Path,
    github: tuple[list[httpx.Request], dict[str, httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = github
    cache = tmp_path / "cache"
    cache.write_text(json.dumps({REPO: 9}))
    cache.chmod(0o600)
    original = gh_app.find_installation

    def find(client: httpx.Client, repository: str) -> int | None:
        assert json.loads(cache.read_text()) == {}
        return original(client, repository)

    monkeypatch.setattr(gh_app, "find_installation", find)
    assert rt.mint(app, REPO, PERMISSIONS, installation_cache=cache).token == TOKEN
    assert [r.url.path for r in requests] == [
        "/app/installations/9/access_tokens",
        f"/repos/{REPO}/installation",
        "/app/installations/7/access_tokens",
    ]
    assert json.loads(cache.read_text()) == {REPO: 7}


@pytest.mark.parametrize("data", ["bad", "[]", '{"x": true}', '{"x": 0}', '{"x": "7"}'])
def test_malformed_cache(app: rt.AppKey, token_file: Path, data: str) -> None:
    token_file.write_text(data)
    assert_kind(
        "token_file",
        lambda: rt.mint(app, REPO, PERMISSIONS, installation_cache=token_file),
    )


def test_unsafe_cache(app: rt.AppKey, token_file: Path) -> None:
    token_file.chmod(0o644)
    assert_kind(
        "token_file",
        lambda: rt.mint(app, REPO, PERMISSIONS, installation_cache=token_file),
    )


@pytest.mark.parametrize(
    "failure", ["missing", "response", "cached_missing", "invalid_key"]
)
def test_mint_failure_is_sanitized(
    app: rt.AppKey,
    tmp_path: Path,
    github: tuple[list[httpx.Request], dict[str, httpx.Response]],
    failure: str,
) -> None:
    _, routes = github
    if failure in {"missing", "cached_missing"}:
        routes.clear()
    else:
        routes["/app/installations/7/access_tokens"] = httpx.Response(
            401, json={"message": TOKEN}
        )
    cache = None
    if failure == "cached_missing":
        cache = tmp_path / "cache"
        cache.write_text(json.dumps({REPO: 9}))
        cache.chmod(0o600)
    if failure == "invalid_key":
        app = rt.AppKey("id", TOKEN)
    assert_kind(
        "installation" if "missing" in failure else "mint",
        lambda: rt.mint(app, REPO, PERMISSIONS, installation_cache=cache),
    )


@pytest.mark.parametrize("step", ["mkstemp", "replace", "fsync"])
def test_cache_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(TOKEN)

    before = set(tmp_path.iterdir())
    monkeypatch.setattr(rt.tempfile if step == "mkstemp" else os, step, fail)
    assert_kind("token_file", lambda: rt._write_cache(tmp_path / "cache", {REPO: 7}))
    assert set(tmp_path.iterdir()) == before


def git_env() -> dict[str, str]:
    executable = shutil.which("git")
    assert executable is not None
    return {
        "PATH": str(Path(executable).parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def test_probe_real_git_pipe(tmp_path: Path) -> None:
    assert rt.USE_PIPE is True
    done = rt._git(
        rt.Minted(TOKEN, 100),
        ["credential", "fill"],
        cwd=tmp_path,
        env=git_env(),
        timeout=10,
        input_text="protocol=https\nhost=github.com\n\n",
    )
    assert done.returncode == 0, done.stderr
    assert f"password={TOKEN}\n" in done.stdout
    assert "username=x-access-token\n" in done.stdout
    assert TOKEN not in repr(done.args)


def test_probe_real_git_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "USE_PIPE", False)
    environment = git_env() | {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "example.retained",
        "GIT_CONFIG_VALUE_0": "yes",
    }
    done = rt.git(
        rt.Minted(TOKEN, 100),
        ["config", "--get", "http.https://github.com/.extraheader"],
        cwd=tmp_path,
        env=environment,
    )
    assert done.returncode == 0
    encoded = done.stdout.strip().removeprefix("AUTHORIZATION: basic ")
    assert base64.b64decode(encoded).decode() == f"x-access-token:{TOKEN}"
    retained = rt.git(
        rt.Minted(TOKEN, 100),
        ["config", "--get", "example.retained"],
        cwd=tmp_path,
        env=environment,
    )
    assert retained.stdout == "yes\n"
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert TOKEN not in repr(done.args)


@pytest.mark.parametrize("pipe", [True, False])
def test_git_invocation_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pipe: bool
) -> None:
    monkeypatch.setattr(rt, "USE_PIPE", pipe)
    executable = shutil.which("git")
    assert executable
    environment = {"PATH": str(Path(executable).parent), "EXPLICIT": "kept"}

    def run(  # noqa: PLR0913  # Assert subprocess keyword arguments.
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        capture_output: bool,
        text: bool,
        pass_fds: tuple[int, ...],
        input: str | None,  # noqa: A002  # subprocess keyword.
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert argv[0] == executable
        assert argv[1:4] == ["-c", "credential.helper=", "-c"]
        assert argv[-1] == "fetch"
        assert TOKEN not in repr(argv)
        assert cwd == tmp_path
        assert timeout == 9
        assert capture_output is True
        assert text is True
        assert check is False
        assert input is None
        if pipe:
            assert env == environment
            assert len(pass_fds) == 1
            assert os.read(pass_fds[0], 1000).decode() == TOKEN
            assert os.read(pass_fds[0], 1000) == b""
            assert argv[4] == "credential.helper=!" + rt.shlex.join(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "tbhbdev.alpha.repo_token",
                    str(pass_fds[0]),
                ]
            )
        else:
            assert pass_fds == ()
            assert env == environment | {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic "
                + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(),
            }
            assert argv[4] == "credential.helper="
        return subprocess.CompletedProcess(argv, 17, "out", "err")

    monkeypatch.setattr(subprocess, "run", run)
    ambient = no_environment()
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", ambient)
        done = rt.git(
            rt.Minted(TOKEN, 100), ["fetch"], cwd=tmp_path, env=environment, timeout=9
        )
    assert (done.returncode, done.stdout, done.stderr) == (17, "out", "err")


@pytest.mark.parametrize(
    "problem",
    ["no_path", "missing_git", "timeout", "oserror", "bad_count", "negative_count"],
)
def test_git_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    environment = git_env()
    if problem == "no_path":
        environment.pop("PATH")
    elif problem == "missing_git":
        environment["PATH"] = str(tmp_path)
    elif "count" in problem:
        monkeypatch.setattr(rt, "USE_PIPE", False)
        environment["GIT_CONFIG_COUNT"] = "bad" if problem == "bad_count" else "-1"
    else:

        def fail(*_args: object, **_kwargs: object) -> NoReturn:
            if problem == "timeout":
                command = "git"
                raise subprocess.TimeoutExpired(command, 1, TOKEN)
            raise OSError(TOKEN)

        monkeypatch.setattr(subprocess, "run", fail)
    assert_kind(
        "git", lambda: rt.git(rt.Minted(TOKEN, 100), [], cwd=tmp_path, env=environment)
    )


@pytest.mark.parametrize(
    "operation", ["get", "store", "erase", "unknown", "missing", "http", "other_host"]
)
def test_helper_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    protocol = "http" if operation == "http" else "https"
    host = "other.example" if operation == "other_host" else "github.com"
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(f"protocol={protocol}\nhost={host}\n\n")
    )
    reader, writer = os.pipe()
    os.write(writer, TOKEN.encode())
    os.close(writer)
    args = [
        str(reader),
        "get" if operation in {"get", "http", "other_host"} else operation,
    ]
    if operation == "missing":
        args = list[str]()
    rt._helper(args)
    if operation == "get":
        assert (
            capsys.readouterr().out == f"username=x-access-token\npassword={TOKEN}\n\n"
        )
    else:
        assert capsys.readouterr().out == ""
        assert os.read(reader, 1000).decode() == TOKEN
        os.close(reader)


def test_app_key_is_frozen(app: rt.AppKey) -> None:
    assert repr(app) == "AppKey()"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(app, "app_id", "changed")  # noqa: B010  # Test frozen enforcement.


@pytest.mark.parametrize("data", [b"\xff", b"{}"])
def test_cache_read_failure(token_file: Path, data: bytes) -> None:
    token_file.write_bytes(data)
    if data == b"{}":
        target = token_file.with_name("original")
        token_file.rename(target)
        token_file.symlink_to(target)
    assert_kind("token_file", lambda: rt._cache(token_file))


def test_success_without_cache(
    app: rt.AppKey, github: tuple[list[httpx.Request], dict[str, httpx.Response]]
) -> None:
    requests, _ = github
    assert rt.mint(app, REPO, PERMISSIONS).token == TOKEN
    assert len(requests) == 2


@pytest.mark.parametrize("mode", [0o700, 0o701, 0o710, 0o755])
def test_safe_parent_modes(token_file: Path, mode: int) -> None:
    token_file.parent.chmod(mode)
    assert rt._read(token_file) == CONNECT_TOKEN + "\n"


def test_root_owned_parent_and_file_owner(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fstat

    def root_parent(descriptor: int) -> os.stat_result:
        found = original(descriptor)
        values = list(found)
        if stat.S_ISDIR(found.st_mode):
            values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", root_parent)
    assert rt._read(token_file) == CONNECT_TOKEN + "\n"
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    assert_kind("token_file", lambda: rt.read_app(token_file, ITEM, endpoints=URLS))


def test_open_descriptors_are_checked_and_closed(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open, real_fstat, real_close = os.open, os.fstat, os.close
    opened: list[int] = []
    checked: list[int] = []
    closed: list[int] = []

    def open_file(path: Path | str, flags: int, *, dir_fd: int | None = None) -> int:
        descriptor = real_open(path, flags, dir_fd=dir_fd)
        opened.append(descriptor)
        assert not os.get_inheritable(descriptor)
        return descriptor

    def check(descriptor: int) -> os.stat_result:
        checked.append(descriptor)
        return real_fstat(descriptor)

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "fstat", check)
    monkeypatch.setattr(os, "close", close)
    assert rt._read(token_file) == CONNECT_TOKEN + "\n"
    assert len(opened) == 2
    assert checked == opened
    assert closed == opened[:1]
    for descriptor in opened:
        with pytest.raises(OSError, match="Bad file descriptor"):
            real_fstat(descriptor)


@pytest.mark.parametrize(
    "request_text",
    [
        "protocol=https\nhost=github.com \n\n",
        "protocol=https\nhost=github.comX\n\n",
        "protocol=https\nhost=github.com\nhost=github.com=other\n\n",
    ],
)
def test_helper_rejects_nearby_hosts(
    request_text: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(request_text))
    rt._helper(["invalid-fd", "get"])
    assert capsys.readouterr() == ("", "")


def test_default_git_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = rt._git

    def run(
        token: rt.Minted,
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 120
        return original(token, args, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(rt, "_git", run)
    assert (
        rt.git(
            rt.Minted(TOKEN, 0), ["--version"], cwd=tmp_path, env=git_env()
        ).returncode
        == 0
    )


def test_cache_accepts_first_installation(token_file: Path) -> None:
    token_file.write_text('{"example/widget": 1}')
    assert rt._cache(token_file) == {REPO: 1}


def test_explicit_backend_preserves_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body: secrets.Json = {"title": "example"}

    def send(
        _self: secrets.Connect,
        url: str,
        method: str,
        path: str,
        received: secrets.Json | None,
    ) -> tuple[httpx.Response, bool]:
        assert (url, method, path, received) == (URLS[0], "POST", "/v1/example", body)
        return httpx.Response(201, json={"created": True}), False

    monkeypatch.setattr(secrets.Connect, "_send", send)
    backend = secrets._ExplicitConnect(CONNECT_TOKEN, [URLS[0]])
    assert backend._call("POST", "/v1/example", body) == {"created": True}
    response, done = backend._send(URLS[0], "POST", "/v1/example", body)
    assert response is not None
    assert response.status_code == 201
    assert done is False
