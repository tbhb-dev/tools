# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha gh-app, with fakes for the clock, Connect and GitHub."""

import base64
import json
import re
import stat
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tbhbdev import alpha, secrets
from tbhbdev.alpha import gh_app

NOW = 1_800_000_000
# The token a fake GitHub mints lives an hour past the fake clock.
EXPIRES_EPOCH = NOW + 3600
EXPIRES = datetime.fromtimestamp(EXPIRES_EPOCH, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
# Split so that gitleaks sees neither a GitHub token nor a key block.
TOKEN = "ghs" + "_FakeInstallationToken0123456789"
BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
END = "-----END " + "RSA PRIVATE KEY-----"
APP_ID = "424242"
REPO = "acme/widget"
SOURCE = Path(gh_app.__file__)
# The names of the 1Password service account's variable, command and token
# file, split for gitleaks.
BANNED = ("OP_SERVICE_" + "ACCOUNT", "op " + "read", "service" + "-account")


@dataclass
class Key:
    private: Path
    public: Path

    @property
    def pem(self) -> str:
        return self.private.read_text()

    @property
    def body(self) -> str:
        return "".join(self.pem.splitlines()[1:-1])


@pytest.fixture(scope="module")
def rsa(tmp_path_factory: pytest.TempPathFactory) -> Key:
    directory = tmp_path_factory.mktemp("rsa")
    private = directory / "private.pem"
    public = directory / "public.pem"
    genpkey = ["openssl", "genpkey", "-algorithm", "RSA", "-out", str(private)]
    subprocess.run([*genpkey, "-pkeyopt", "rsa_keygen_bits:2048"], check=True)
    subprocess.run(
        ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
    )
    return Key(private, public)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail at once on any request that no MockTransport answers."""

    def refuse(*_args: object, **_kwargs: object) -> httpx.Response:
        message = "a test reached the real network"
        raise AssertionError(message)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    # A client with no transport of its own looks up the host's proxies first,
    # which crashes a forked mutmut worker on macOS before it can send anything.
    monkeypatch.setattr("httpx._utils.getproxies", refuse)


@dataclass
class Store:
    values: dict[str, str]
    reads: list[str] = field(default_factory=list)
    created: int = 0

    def factory(self) -> Store:
        self.created += 1
        return self

    def read(self, ref: str, /) -> str:
        self.reads.append(ref)
        if ref not in self.values:
            message = f"{ref!r} is missing"
            raise secrets.SecretsError(message)
        return self.values[ref]


def app_store(key: str, *, app: str = "bot", vault: str = "tbhb-dev") -> Store:
    base = f"op://{vault}/github-app-{app}"
    return Store({f"{base}/app_id": APP_ID, f"{base}/private_key": key})


def ok_routes(installation: int = 7) -> dict[tuple[str, str], httpx.Response]:
    return {
        ("GET", f"/repos/{REPO}/installation"): httpx.Response(
            200, json={"id": installation}
        ),
        ("POST", f"/app/installations/{installation}/access_tokens"): httpx.Response(
            201, json={"token": TOKEN, "expires_at": EXPIRES}
        ),
    }


class Github:
    """The GitHub API: answers each route and 404s the rest."""

    def __init__(
        self, routes: dict[tuple[str, str], httpx.Response] | None = None
    ) -> None:
        self.routes = ok_routes() if routes is None else routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        return self.routes.get(key, httpx.Response(404, json={"message": "Not Found"}))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def jwt(self) -> str:
        return self.requests[0].headers["Authorization"].removeprefix("Bearer ")


def env_for(
    tmp_path: Path,
    store: Store,
    github: Github,
    *,
    now: int = NOW,
    openssl: gh_app.OpenSsl = gh_app.run_openssl,
) -> gh_app.Env:
    return gh_app.Env(
        tmp_path / "state", lambda: now, store.factory, openssl, github.transport
    )


def run(
    capsys: pytest.CaptureFixture[str], env: gh_app.Env, *argv: str
) -> tuple[int, str, str]:
    code = gh_app.main(list(argv), "gh-app", env=env)
    out, err = capsys.readouterr()
    return code, out, err


def out_path(tmp_path: Path, *parts: str) -> Path:
    return tmp_path.joinpath("state", "bot", "acme", *parts)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- pure functions ---------------------------------------------------------


BASE = gh_app.Args(
    app="bot",
    repo=REPO,
    fresh=False,
    dry_run=False,
    all_repos=False,
    vault="tbhb-dev",
    cooldown_until=None,
    cooldown_status=False,
)


def test_parse_args_fills_every_field() -> None:
    assert gh_app.parse_args(["bot", REPO]) == BASE
    full = gh_app.parse_args(
        ["bot", REPO, "--fresh", "--dry-run", "--all-repos", "--vault", "v"]
    )
    assert full == replace(BASE, fresh=True, dry_run=True, all_repos=True, vault="v")
    until = gh_app.parse_args(["bot", "--cooldown-until", "5"])
    assert until == replace(BASE, repo="", cooldown_until=5)
    status = gh_app.parse_args(["bot", "--cooldown-status"])
    assert status == replace(BASE, repo="", cooldown_status=True)


@pytest.mark.parametrize("app", ["a", "ab-", "Ab", "a_b", "a.b", "a b", ""])
def test_a_bad_app_is_a_usage_error(app: str) -> None:
    with pytest.raises(gh_app.UsageError, match="must match"):
        gh_app.parse_args([app, REPO])


@pytest.mark.parametrize("app", ["ab", "a1", "my-app-2"])
def test_a_good_app_parses(app: str) -> None:
    assert gh_app.parse_args([app, REPO]).app == app


@pytest.mark.parametrize(
    "repo", ["acme", "a/b/c", "../r", "o/..", "o/r r", "o/r;x", "/r", "o/", ""]
)
def test_a_bad_repo_is_a_usage_error(repo: str) -> None:
    with pytest.raises(gh_app.UsageError, match="OWNER/REPO"):
        gh_app.parse_args(["bot", repo])


def test_dots_are_fine_inside_a_repo_name() -> None:
    assert gh_app.parse_args(["bot", "a.b/c..d"]).repo == "a.b/c..d"


@pytest.mark.parametrize(
    "argv",
    [
        ["bot"],
        [],
        ["-ab", REPO],
        ["bot", REPO, "--nope"],
        ["bot", REPO, "extra"],
        ["bot", "--cooldown-until"],
        ["bot", "--cooldown-until", "abc"],
        ["bot", "--cooldown-until", "-1"],
        ["bot", "--cooldown-until", "١٢٣"],
        ["bot", "--cooldown-until", "100000000000"],
        ["bot", "--cooldown-until", "5", "--cooldown-status"],
        ["bot", REPO, "--cooldown-until", "5"],
        ["bot", REPO, "--cooldown-status"],
        ["bot", "--cooldown-status", "--fresh"],
        ["bot", "--cooldown-status", "--dry-run"],
        ["bot", "--cooldown-until", "5", "--all-repos"],
    ],
)
def test_other_bad_command_lines_are_usage_errors(argv: list[str]) -> None:
    with pytest.raises(gh_app.UsageError):
        gh_app.parse_args(argv)


@pytest.mark.parametrize(
    ("text", "epoch"),
    [
        (None, None),
        ("", None),
        ("12\n", 12),
        (" 12 ", 12),
        ("0", 0),
        ("99999999999", 99999999999),
        ("100000000000", None),
        ("-1", None),
        ("+1", None),
        ("1_0", None),
        ("1.5", None),
        ("١٢", None),
        ("12x", None),
        ("x12", None),
    ],
)
def test_parse_epoch(text: str | None, epoch: int | None) -> None:
    assert gh_app.parse_epoch(text) == epoch


def test_state_root() -> None:
    home = Path("/home/u")
    assert gh_app.state_root({"XDG_STATE_HOME": "/x"}, home) == Path("/x/gh-app")
    assert gh_app.state_root({}, home) == Path("/home/u/.local/state/gh-app")
    assert gh_app.state_root({"XDG_STATE_HOME": ""}, home) == Path(
        "/home/u/.local/state/gh-app"
    )


def test_layout() -> None:
    state = Path("/s")
    assert gh_app.out_dir(state, "bot", "o/r", all_repos=False) == Path("/s/bot/o/r")
    assert gh_app.out_dir(state, "bot", "o/r", all_repos=True) == Path(
        "/s/bot/o/_all-repos"
    )
    assert gh_app.cooldown_path(state, "bot") == Path("/s/cooldown/bot")


@pytest.mark.parametrize(
    ("text", "until"),
    [
        (None, None),
        ("junk", None),
        ("99", None),
        ("100\n", None),
        ("101\n", 101),
        ("5000", 5000),
    ],
)
def test_cooldown_decision(text: str | None, until: int | None) -> None:
    assert gh_app.cooldown_until(text, 100) == until


@pytest.mark.parametrize(
    ("expires", "fresh", "reuse"),
    [
        (str(NOW + 601), False, True),
        (f"{NOW + 601}\n", False, True),
        (str(NOW + 600), False, False),
        (str(NOW + 599), False, False),
        (str(NOW - 5), False, False),
        (str(NOW + 601), True, False),
        ("0", False, False),
        ("junk", False, False),
        (None, False, False),
    ],
)
def test_reuse_decision(expires: str | None, fresh: bool, reuse: bool) -> None:
    assert gh_app.reusable(expires, NOW, fresh=fresh) is reuse


def test_utc() -> None:
    assert gh_app.utc(0) == "1970-01-01T00:00:00Z"
    assert gh_app.utc(EXPIRES_EPOCH) == EXPIRES
    assert gh_app.cooldown_message(0) == "cooldown until 1970-01-01T00:00:00Z"


def test_rebuild_pem_refolds_a_flattened_key() -> None:
    body = "A" * 64 + "B" * 64 + "C" * 3
    flat = f"{BEGIN} {body[:50]}  {body[50:]} \n{END}"
    assert gh_app.rebuild_pem(flat) == (
        f"{BEGIN}\n{'A' * 64}\n{'B' * 64}\nCCC\n{END}\n"
    )


def test_rebuild_pem_keeps_a_proper_key_and_the_label(rsa: Key) -> None:
    assert gh_app.rebuild_pem(rsa.pem) == rsa.pem
    flat = rsa.pem.replace("\n", " ")
    assert gh_app.rebuild_pem(flat) == rsa.pem


def test_rebuild_pem_leaves_other_text_to_openssl() -> None:
    assert gh_app.rebuild_pem("not a key") == "not a key"
    assert gh_app.rebuild_pem("-----BEGIN A-----x-----END B-----") == (
        "-----BEGIN A-----x-----END B-----"
    )


def decode(part: str) -> object:
    assert "=" not in part
    assert re.fullmatch(r"[A-Za-z0-9_-]*", part)
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_signing_input() -> None:
    header, payload = gh_app.signing_input("123", NOW).decode().split(".")
    assert decode(header) == {"alg": "RS256", "typ": "JWT"}
    assert decode(payload) == {"iat": NOW - 60, "exp": NOW + 540, "iss": "123"}
    raw = gh_app.signing_input("123", NOW).decode().split(".")
    assert base64.urlsafe_b64decode(raw[0] + "==") == b'{"alg":"RS256","typ":"JWT"}'
    assert base64.urlsafe_b64decode(raw[1] + "==") == (
        f'{{"iat":{NOW - 60},"exp":{NOW + 540},"iss":"123"}}'.encode()
    )


def test_b64url_drops_padding_and_uses_the_url_alphabet() -> None:
    assert gh_app.b64url(b"\xfb\xff") == "-_8"
    assert gh_app.b64url(b"a") == "YQ"
    assert gh_app.b64url(b"\x00\x00\x17") == "AAA" + "X"
    assert gh_app.b64url(b"") == ""


def test_the_owner_is_what_precedes_the_first_slash() -> None:
    assert gh_app.split_repo("o/r") == ("o", "r")
    assert gh_app.split_repo("a/b/c") == ("a", "b/c")
    assert gh_app.lookup_paths("a/b/c")[1] == "/orgs/a/installation"
    assert gh_app.token_body("a/b/c", all_repos=False) == {"repositories": ["b/c"]}
    assert gh_app.out_dir(Path("/s"), "bot", "a/b", all_repos=False) == Path(
        "/s/bot/a/b"
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["Bad_App", REPO], "app 'Bad_App' must match [a-z0-9][a-z0-9-]*[a-z0-9]"),
        (["bot", "nope"], "repository 'nope' must be OWNER/REPO"),
        (["bot", "o/.."], "repository 'o/..' must be OWNER/REPO"),
        (["bot"], "OWNER/REPO is required"),
        (
            ["bot", "--cooldown-until", "x"],
            "--cooldown-until needs a whole number of seconds since the epoch",
        ),
        (
            ["bot", "--cooldown-until", "5", "--cooldown-status"],
            "--cooldown-until and --cooldown-status are exclusive",
        ),
        (["bot", REPO, "--cooldown-status"], "a cooldown option takes only the app"),
        (
            ["bot", "--cooldown-until", "5", "--fresh"],
            "a cooldown option takes only the app",
        ),
    ],
)
def test_usage_errors_say_what_is_wrong(argv: list[str], message: str) -> None:
    with pytest.raises(gh_app.UsageError) as caught:
        gh_app.parse_args(argv)
    assert str(caught.value) == message


def test_request_paths_and_bodies() -> None:
    assert gh_app.lookup_paths("o/r") == [
        "/repos/o/r/installation",
        "/orgs/o/installation",
    ]
    assert gh_app.token_body("o/r", all_repos=False) == {"repositories": ["r"]}
    assert gh_app.token_body("o/r", all_repos=True) == {}


@pytest.mark.parametrize(
    ("response", "ident"),
    [
        (httpx.Response(200, json={"id": 7}), 7),
        (httpx.Response(200, json={"id": "7"}), None),
        (httpx.Response(200, json={}), None),
        (httpx.Response(200, json=[7]), None),
        (httpx.Response(200, content=b"not json"), None),
        (httpx.Response(404, json={"id": 7}), None),
        (httpx.Response(201, json={"id": 7}), None),
    ],
)
def test_installation_id(response: httpx.Response, ident: int | None) -> None:
    assert gh_app.installation_id(response) == ident


@pytest.mark.parametrize(
    "expires",
    ["2026-11-09T11:00:00Z", "2026-11-09T11:00:00+00:00", "2026-11-09T06:00:00-05:00"],
)
def test_parse_token(expires: str) -> None:
    minted = gh_app.parse_token({"token": TOKEN, "expires_at": expires})
    assert minted.token == TOKEN
    assert minted.expires_at == int(datetime(2026, 11, 9, 11, tzinfo=UTC).timestamp())


def test_a_minted_token_stays_out_of_its_repr() -> None:
    assert TOKEN not in repr(gh_app.Minted(TOKEN, 5))
    assert repr(gh_app.Minted(TOKEN, 5)) == "Minted(expires_at=5)"


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"message": "Bad credentials", "token": "x y"}, "Bad credentials"),
        (
            {"message": "Bad credentials", "documentation_url": "https://x"},
            "Bad credentials",
        ),
        ({"token": TOKEN}, "no message"),
        ({"expires_at": EXPIRES}, "no message"),
        ({"token": 5, "expires_at": EXPIRES, "message": ""}, "no message"),
        ({"token": TOKEN, "expires_at": 5, "message": 5}, "no message"),
        ({"token": TOKEN, "expires_at": "tomorrow"}, "no message"),
        ({"token": TOKEN, "expires_at": "2026-11-09T11:00:00"}, "no message"),
        ({"token": "bad token\nx", "expires_at": EXPIRES}, "no message"),
        ({"token": "", "expires_at": EXPIRES}, "no message"),
        ({"token": "a:b", "expires_at": EXPIRES}, "no message"),
        ({}, "no message"),
    ],
)
def test_parse_token_quotes_only_the_message_from_github(
    payload: gh_app.Json, detail: str
) -> None:
    with pytest.raises(gh_app.GhAppError) as caught:
        gh_app.parse_token(payload)
    assert str(caught.value) == f"GitHub did not return a token: {detail}"


def test_render_hosts() -> None:
    assert gh_app.render_hosts("bot", TOKEN) == (
        "github.com:\n"
        "    users:\n"
        '        "bot[bot]":\n'
        f"            oauth_token: {TOKEN}\n"
        "    git_protocol: https\n"
        f"    oauth_token: {TOKEN}\n"
        '    user: "bot[bot]"\n'
    )


# --- the thin layer ---------------------------------------------------------


def test_default_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    env = gh_app.default_env()
    assert env.state == tmp_path / "gh-app"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert gh_app.default_env().state == tmp_path / "home/.local/state/gh-app"
    assert env.store is secrets.Store
    assert env.openssl is gh_app.run_openssl
    assert env.transport is None
    assert abs(env.now() - time.time()) < 5


def test_run_openssl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    done = gh_app.run_openssl(["dgst", "-sha256"], b"abc")
    assert done.returncode == 0
    assert (
        b"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        in done.stdout
    )
    assert (
        gh_app.run_openssl(["pkey", "-in", str(tmp_path / "none")], b"").returncode != 0
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(gh_app.GhAppError, match="openssl isn't installed"):
        gh_app.run_openssl(["version"], b"")


def test_run_openssl_runs_openssl_with_its_input_and_captures_its_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        argv: list[str], **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gh_app.run_openssl(["version"], b"in")
    gh_app.run_openssl(["version"])
    assert calls == [
        (
            ["openssl", "version"],
            {"input": b"in", "capture_output": True, "check": False},
        ),
        (
            ["openssl", "version"],
            {"input": None, "capture_output": True, "check": False},
        ),
    ]


def test_alpha_lists_and_runs_gh_app(capsys: pytest.CaptureFixture[str]) -> None:
    assert "\n  gh-app " in alpha.USAGE
    assert alpha.COMMANDS["gh-app"] is alpha.gh_app_command
    assert alpha.main(["gh-app", "--help"], "tbhbdev alpha") == 0
    assert capsys.readouterr() == (gh_app.USAGE.format(prog="tbhbdev alpha gh-app"), "")


def test_the_parser_raises_where_argparse_would_print_help() -> None:
    with pytest.raises(gh_app.HelpRequestedError):
        gh_app.Parser().print_help()


@pytest.mark.parametrize(
    "argv", [["-h"], ["--help"], ["bot", REPO, "-h"], ["bot", "--help", "--fresh"]]
)
def test_help(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    assert gh_app.main(argv, "p") == 0
    assert capsys.readouterr() == (gh_app.USAGE.format(prog="p"), "")
    with pytest.raises(gh_app.HelpRequestedError):
        gh_app.parse_args(argv)


def test_a_usage_error_prints_usage_and_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store, github = app_store("k"), Github()
    env = env_for(tmp_path, store, github)
    code, out, err = run(capsys, env, "Bad_App", REPO)
    assert (code, out) == (2, "")
    assert err.startswith("gh-app: app 'Bad_App' must match ")
    assert err.endswith(gh_app.USAGE.format(prog="gh-app"))
    assert (store.created, github.requests) == (0, [])
    assert run(capsys, env, "bot")[0] == 2
    assert run(capsys, env, "bot", "nope")[0] == 2


def test_default_main_builds_its_own_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert gh_app.main(["bot", "--cooldown-status"], "gh-app") == 0
    assert capsys.readouterr() == ("no cooldown\n", "")


def test_a_mint_writes_hosts_yml_and_prints_only_the_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store, github = app_store(rsa.pem), Github()
    code, out, err = run(capsys, env_for(tmp_path, store, github), "bot", REPO)
    out_dir = out_path(tmp_path, "widget")
    assert (code, out, err) == (0, f"{out_dir}\n", "")
    assert (out_dir / "hosts.yml").read_text() == gh_app.render_hosts("bot", TOKEN)
    assert (out_dir / "app-slug").read_text() == "bot\n"
    assert (out_dir / "expires-at").read_text() == f"{EXPIRES_EPOCH}\n"
    assert mode(out_dir) == 0o700
    for name in ("hosts.yml", "app-slug", "expires-at"):
        assert mode(out_dir / name) == 0o600
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "app-slug",
        "expires-at",
        "hosts.yml",
    ]
    assert store.reads == [
        "op://tbhb-dev/github-app-bot/app_id",
        "op://tbhb-dev/github-app-bot/private_key",
    ]


def test_a_mint_makes_the_documented_requests(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    run(capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO)
    lookup, create = github.requests
    assert (lookup.method, str(lookup.url)) == (
        "GET",
        "https://api.github.com/repos/acme/widget/installation",
    )
    assert (create.method, str(create.url)) == (
        "POST",
        "https://api.github.com/app/installations/7/access_tokens",
    )
    assert json.loads(create.content) == {"repositories": ["widget"]}
    for request in (lookup, create):
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.headers["Authorization"] == f"Bearer {github.jwt}"
        names = {name for name, _ in request.headers.raw}
        assert {b"Accept", b"X-GitHub-Api-Version", b"Authorization"} <= names
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }


def verify(jwt: str, key: Key, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    head, payload, signature = jwt.split(".")
    data = tmp_path / "data"
    sig = tmp_path / "sig"
    data.write_text(f"{head}.{payload}")
    sig.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    return subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(key.public),
            "-signature",
            str(sig),
            str(data),
        ],
        capture_output=True,
        check=False,
    )


def test_the_jwt_is_a_real_rs256_signature(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    store = Store(
        {
            "op://tbhb-dev/github-app-bot/app_id": f" {APP_ID}\n",
            "op://tbhb-dev/github-app-bot/private_key": rsa.pem,
        }
    )
    run(capsys, env_for(tmp_path, store, github), "bot", REPO)
    head, payload, _ = github.jwt.split(".")
    assert decode(head) == {"alg": "RS256", "typ": "JWT"}
    assert decode(payload) == {"iat": NOW - 60, "exp": NOW + 540, "iss": APP_ID}
    assert verify(github.jwt, rsa, tmp_path).returncode == 0
    head, payload, signature = github.jwt.split(".")
    assert verify(f"{head}x.{payload}.{signature}", rsa, tmp_path).returncode != 0


def test_a_flattened_key_is_rebuilt_before_signing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    flat = rsa.pem.replace("\n", " ")
    code, _, _ = run(capsys, env_for(tmp_path, app_store(flat), github), "bot", REPO)
    assert code == 0
    assert verify(github.jwt, rsa, tmp_path).returncode == 0


def test_the_key_never_reaches_argv_and_its_file_is_private_and_gone(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    calls: list[tuple[list[str], int | None]] = []
    keyfiles: list[Path] = []

    def spy(
        argv: list[str], data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        keyfile = Path(argv[argv.index("-in") + 1] if "-in" in argv else argv[-1])
        keyfiles.append(keyfile)
        assert mode(keyfile) == 0o600
        assert mode(keyfile.parent) == 0o700
        assert keyfile.read_text() == rsa.pem
        assert rsa.body[:30] not in " ".join(argv)
        assert rsa.body[:30].encode() not in (data or b"")
        calls.append((argv, None))
        return gh_app.run_openssl(argv, data)

    env = env_for(tmp_path, app_store(rsa.pem), Github(), openssl=spy)
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert [argv[:2] for argv, _ in calls] == [["pkey", "-in"], ["dgst", "-sha256"]]
    assert calls[0][0][3:] == ["-noout"]
    assert calls[1][0][2] == "-sign"
    assert len({k.parent for k in keyfiles}) == 1
    assert not keyfiles[0].parent.exists()


def test_the_key_file_is_gone_after_a_parse_failure(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    seen: list[Path] = []

    def spy(
        argv: list[str], data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        seen.append(Path(argv[2]))
        return gh_app.run_openssl(argv, data)

    github = Github()
    env = env_for(tmp_path, app_store("not a key"), github, openssl=spy)
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out, err) == (1, "", "private_key does not parse\n")
    assert github.requests == []
    assert not seen[0].parent.exists()
    assert not out_path(tmp_path).exists()


@pytest.mark.parametrize(("returncode", "stdout"), [(1, b"sig"), (0, b"")])
def test_a_failed_signature_is_an_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    returncode: int,
    stdout: bytes,
) -> None:
    def fake(
        argv: list[str], _data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "pkey":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, returncode, stdout, b"")

    github = Github()
    env = env_for(tmp_path, app_store(rsa.pem), github, openssl=fake)
    assert run(capsys, env, "bot", REPO) == (1, "", "cannot sign the JWT\n")
    assert github.requests == []


def test_openssl_missing_is_an_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa: Key,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    env = env_for(tmp_path, app_store(rsa.pem), Github())
    assert run(capsys, env, "bot", REPO) == (1, "", "openssl isn't installed\n")


@pytest.mark.parametrize("missing", ["app_id", "private_key"])
@pytest.mark.parametrize("how", ["absent", "empty", "blank"])
def test_an_unreadable_field_is_an_error_that_names_no_value(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    missing: str,
    how: str,
) -> None:
    store = app_store(rsa.pem)
    ref = f"op://tbhb-dev/github-app-bot/{missing}"
    if how == "absent":
        del store.values[ref]
    else:
        store.values[ref] = "" if how == "empty" else " \n"
    github = Github()
    code, out, err = run(capsys, env_for(tmp_path, store, github), "bot", REPO)
    assert (code, out) == (1, "")
    assert err == f"cannot read {missing} from tbhb-dev/github-app-bot\n"
    assert github.requests == []
    assert not out_path(tmp_path).exists()


def test_the_vault_can_be_overridden(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store = app_store(rsa.pem, vault="other")
    env = env_for(tmp_path, store, Github())
    assert run(capsys, env, "bot", REPO, "--vault", "other")[0] == 0
    assert store.reads[0] == "op://other/github-app-bot/app_id"
    env = env_for(tmp_path, store, Github())
    code, _, err = run(capsys, env, "bot", REPO, "--fresh")
    assert (code, err) == (1, "cannot read app_id from tbhb-dev/github-app-bot\n")


def test_the_installation_falls_back_from_the_repo_to_the_org(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github(
        {
            ("GET", "/orgs/acme/installation"): httpx.Response(200, json={"id": 9}),
            **{k: v for k, v in ok_routes(9).items() if k[0] == "POST"},
        }
    )
    code, _, _ = run(capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO)
    assert code == 0
    assert [(r.method, r.url.path) for r in github.requests] == [
        ("GET", "/repos/acme/widget/installation"),
        ("GET", "/orgs/acme/installation"),
        ("POST", "/app/installations/9/access_tokens"),
    ]


def test_no_installation_is_an_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github({})
    code, out, err = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO
    )
    assert (code, out) == (1, "")
    assert err == "app bot has no installation for acme/widget or acme\n"
    assert [r.method for r in github.requests] == ["GET", "GET"]
    assert not out_path(tmp_path).exists()


def test_all_repos_asks_for_every_repo_and_uses_its_own_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    code, out, _ = run(
        capsys,
        env_for(tmp_path, app_store(rsa.pem), github),
        "bot",
        REPO,
        "--all-repos",
    )
    assert code == 0
    assert out == f"{out_path(tmp_path, '_all-repos')}\n"
    assert json.loads(github.requests[-1].content) == {}
    assert (out_path(tmp_path, "_all-repos") / "hosts.yml").is_file()
    assert not out_path(tmp_path, "widget").exists()


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (
            httpx.Response(401, json={"message": "Bad credentials", "token": "no"}),
            "Bad credentials",
        ),
        (httpx.Response(201, json={"token": TOKEN}), "no message"),
        (httpx.Response(500, content=b"<html>oops"), "no message"),
    ],
)
def test_a_bad_token_reply_quotes_only_the_message(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    reply: httpx.Response,
    detail: str,
) -> None:
    github = Github(
        {**ok_routes(), ("POST", "/app/installations/7/access_tokens"): reply}
    )
    code, out, err = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO
    )
    assert (code, out) == (1, "")
    assert err == f"GitHub did not return a token: {detail}\n"
    assert not out_path(tmp_path).exists()


def test_a_network_failure_names_only_its_type(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        message = f"refused {request.headers['Authorization']}"
        raise httpx.ConnectError(message)

    env = gh_app.Env(
        tmp_path / "state",
        lambda: NOW,
        app_store(rsa.pem).factory,
        gh_app.run_openssl,
        httpx.MockTransport(boom),
    )
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out) == (1, "")
    assert err == "the GitHub request failed: ConnectError\n"


def test_nothing_secret_reaches_the_output(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github = Github()
    store = app_store(rsa.pem)
    seen = ""
    extras: list[list[str]] = [[], ["--dry-run"], ["--all-repos"]]
    for extra in extras:
        code, out, err = run(
            capsys, env_for(tmp_path, store, github), "bot", REPO, *extra
        )
        assert code == 0
        seen += out + err
    failing = Github(
        {
            **ok_routes(),
            ("POST", "/app/installations/7/access_tokens"): httpx.Response(
                401, json={"message": "no"}
            ),
        }
    )
    seen += "".join(
        run(capsys, env_for(tmp_path, store, failing), "bot", REPO, "--fresh")[1:]
    )
    seen += "".join(
        run(
            capsys, env_for(tmp_path, app_store("junk"), github), "bot", REPO, "--fresh"
        )[1:]
    )
    for secret in (
        TOKEN,
        rsa.body[:40],
        rsa.body[-40:],
        github.jwt,
        github.jwt.split(".")[2],
    ):
        assert secret not in seen
    assert "BEGIN" not in seen


def test_fresh_always_mints_and_cannot_be_reused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    github, store = Github(), app_store(rsa.pem)
    env = env_for(tmp_path, store, github)
    out_dir = out_path(tmp_path, "widget")
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert (out_dir / "expires-at").read_text() == f"{EXPIRES_EPOCH}\n"
    assert len(github.requests) == 2
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert len(github.requests) == 2
    assert run(capsys, env, "bot", REPO, "--fresh")[0] == 0
    assert (out_dir / "expires-at").read_text() == "0\n"
    assert len(github.requests) == 4
    assert run(capsys, env, "bot", REPO)[0] == 0
    assert len(github.requests) == 6


def seed(tmp_path: Path, expires: bytes | None, *, hosts: bool = True) -> Path:
    out_dir = out_path(tmp_path, "widget")
    out_dir.mkdir(parents=True)
    if hosts:
        (out_dir / "hosts.yml").write_text("old\n")
    if expires is not None:
        (out_dir / "expires-at").write_bytes(expires)
    return out_dir


def test_a_live_token_is_reused_without_reading_secrets(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    out_dir = seed(tmp_path, f"{NOW + 601}\n".encode())
    store, github = app_store(rsa.pem), Github()
    code, out, err = run(capsys, env_for(tmp_path, store, github), "bot", REPO)
    assert (code, out, err) == (0, f"{out_dir}\n", "")
    assert (store.created, store.reads, github.requests) == (0, [], [])
    assert (out_dir / "hosts.yml").read_text() == "old\n"


@pytest.mark.parametrize(
    ("expires", "hosts"),
    [
        (str(NOW + 600).encode(), True),
        (str(NOW + 599).encode(), True),
        (b"junk", True),
        (b"\xff\xfe", True),
        (None, True),
        (str(NOW + 601).encode(), False),
    ],
)
def test_a_token_that_cannot_be_reused_is_minted_again(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa: Key,
    expires: bytes | None,
    hosts: bool,
) -> None:
    out_dir = seed(tmp_path, expires, hosts=hosts)
    github = Github()
    code, out, _ = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), github), "bot", REPO
    )
    assert (code, out) == (0, f"{out_dir}\n")
    assert len(github.requests) == 2
    assert (out_dir / "hosts.yml").read_text() == gh_app.render_hosts("bot", TOKEN)


def test_the_state_file_is_replaced_atomically(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa: Key,
) -> None:
    out_dir = seed(tmp_path, b"0")
    replaced: list[tuple[str, str, str]] = []
    real = Path.replace

    def spy(self: Path, target: Path) -> Path:
        replaced.append((self.name, Path(target).name, self.read_text()[:11]))
        assert mode(self) == 0o600
        assert self.parent == Path(target).parent
        if self.name.startswith(".hosts.yml."):
            assert (out_dir / "hosts.yml").read_text() == "old\n"
        return real(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    code, _, _ = run(
        capsys, env_for(tmp_path, app_store(rsa.pem), Github()), "bot", REPO
    )
    assert code == 0
    assert [(a.split(".")[1], b) for a, b, _ in replaced] == [
        ("hosts", "hosts.yml"),
        ("app-slug", "app-slug"),
        ("expires-at", "expires-at"),
    ]
    assert replaced[0][2] == "github.com:"


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rsa: Key,
) -> None:
    out_dir = seed(tmp_path, b"0")

    def fail(_self: Path, target: Path) -> Path:
        raise OSError(28, "No space left on device", str(target))

    monkeypatch.setattr(Path, "replace", fail)
    env = env_for(tmp_path, app_store(rsa.pem), Github())
    code, out, err = run(capsys, env, "bot", REPO)
    assert (code, out) == (1, "")
    assert err == f"cannot write {out_dir / 'hosts.yml'}: No space left on device\n"
    assert (out_dir / "hosts.yml").read_text() == "old\n"
    assert sorted(p.name for p in out_dir.iterdir()) == ["expires-at", "hosts.yml"]
    assert TOKEN not in err


def test_cooldown_until_writes_one_epoch_atomically(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store, github = app_store("k"), Github()
    env = env_for(tmp_path, store, github)
    path = tmp_path / "state" / "cooldown" / "bot"
    assert run(capsys, env, "bot", "--cooldown-until", str(NOW + 5)) == (0, "", "")
    assert path.read_text() == f"{NOW + 5}\n"
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert run(capsys, env, "bot", "--cooldown-until", "7")[0] == 0
    assert path.read_text() == "7\n"
    assert [p.name for p in path.parent.iterdir()] == ["bot"]
    assert (store.created, github.requests) == (0, [])


def test_an_active_cooldown_refuses_every_mint_before_any_read(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store, github = app_store(rsa.pem), Github()
    env = env_for(tmp_path, store, github)
    seed(tmp_path, f"{NOW + 9999}".encode())
    run(capsys, env, "bot", "--cooldown-until", str(NOW + 1))
    extras: list[list[str]] = [[], ["--fresh"], ["--dry-run"], ["--all-repos"]]
    for extra in extras:
        code, out, err = run(capsys, env, "bot", REPO, *extra)
        assert (code, out) == (75, "")
        assert err == f"cooldown until {gh_app.utc(NOW + 1)}\n"
    assert (store.created, store.reads, github.requests) == (0, [], [])


def test_a_cooldown_belongs_to_one_app(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    env = env_for(tmp_path, app_store(rsa.pem, app="other"), Github())
    run(capsys, env, "bot", "--cooldown-until", str(NOW + 1))
    assert run(capsys, env, "other", REPO, "--dry-run")[0] == 0


@pytest.mark.parametrize("until", [NOW, NOW - 1, 0])
def test_an_expired_cooldown_lets_a_mint_through(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key, until: int
) -> None:
    env = env_for(tmp_path, app_store(rsa.pem), Github())
    run(capsys, env, "bot", "--cooldown-until", str(until))
    assert run(capsys, env, "bot", REPO)[0] == 0


def test_cooldown_status(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    env = env_for(tmp_path, app_store("k"), Github())
    assert run(capsys, env, "bot", "--cooldown-status") == (0, "no cooldown\n", "")
    run(capsys, env, "bot", "--cooldown-until", str(NOW + 1))
    assert run(capsys, env, "bot", "--cooldown-status") == (
        75,
        f"cooldown until {gh_app.utc(NOW + 1)}\n",
        "",
    )
    run(capsys, env, "bot", "--cooldown-until", str(NOW))
    assert run(capsys, env, "bot", "--cooldown-status") == (0, "no cooldown\n", "")


def test_dry_run_signs_without_a_request_or_state(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, rsa: Key
) -> None:
    store, github = app_store(rsa.pem), Github()
    out_dir = seed(tmp_path, f"{NOW + 9999}".encode())
    code, out, err = run(
        capsys, env_for(tmp_path, store, github), "bot", REPO, "--dry-run"
    )
    assert (code, err) == (0, "")
    assert out == (
        "dry run: read app_id and private_key for bot, "
        "signed a JWT and made no request\n"
    )
    assert github.requests == []
    assert len(store.reads) == 2
    assert (out_dir / "hosts.yml").read_text() == "old\n"
    assert sorted(p.name for p in out_dir.iterdir()) == ["expires-at", "hosts.yml"]


def test_dry_run_still_validates_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env = env_for(tmp_path, app_store("junk"), Github())
    assert run(capsys, env, "bot", REPO, "--dry-run") == (
        1,
        "",
        "private_key does not parse\n",
    )


def test_the_module_names_no_service_account() -> None:
    source = SOURCE.read_text()
    for banned in BANNED:
        assert banned not in source
