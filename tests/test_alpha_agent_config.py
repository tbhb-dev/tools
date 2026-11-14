"""Agent config parsing is a table over literal values, with no file read."""

from __future__ import annotations

import copy
import dataclasses
from typing import cast

import pytest

from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.decisions import config
from tbhbdev.alpha.agent.model import Invalid

type Table = dict[str, object]

MISSING = object()
EMPTY: list[str] = []
REFERENCE_KEYS = [
    f.name for f in dataclasses.fields(model.Credentials) if f.name != "account"
]
NAME_RULE = "[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
HOST_RULE = (
    r"(?=.{1,63}\Z)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
)
REFERENCE = "must be a vault/item/field reference"
PATHS = "must not contain a NUL or '..' part"
NON_BLANK = "must be a non-blank string"
STRINGS = "must be a non-empty list of non-blank strings"
POSITIVE = "must be a positive integer"
HTTPS = "must be an https URL with no credentials, path, query or fragment"
OTEL_HOST = "otlp.example.net"
DURATION = "must be bare seconds or a number ending in s, m or h, up to 7 days"


@pytest.mark.parametrize("entries", [[], ["tbhb-dev/tools", "github.com/a/b"]])
def test_public_repositories(entries: list[str]) -> None:
    assert config.parse(GOOD | {"public_repos": entries}).public_repos == tuple(entries)
    assert config.parse(GOOD).public_repos == ()


def with_otel(table: Table) -> Table:
    sandbox = GOOD["sandbox"]
    assert isinstance(sandbox, dict)
    return GOOD | {
        "sandbox": sandbox | {"network_domains": [OTEL_HOST]},
        "otel": table,
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://otlp.example.net:4318",
        "https://otlp.example.net/",
        "https://otlp.example.net:65535",
        "https://otlp.example.net:1",
    ],
)
def test_otel_endpoint(endpoint: str) -> None:
    parsed = config.parse(with_otel({"endpoint": endpoint}))
    assert parsed.otel_endpoint == endpoint
    assert config.parse(GOOD).otel_endpoint is None


@pytest.mark.parametrize(
    ("table", "reason"),
    [
        ({"endpoint": "http://otlp.example.net"}, HTTPS),
        ({"endpoint": "https://user:pw@otlp.example.net"}, HTTPS),
        ({"endpoint": "https://user@otlp.example.net"}, HTTPS),
        ({"endpoint": "https://:pw@otlp.example.net"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/token"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/a b"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/a\x00b"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/a\x01b"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/a\x7fb"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/\u00e9"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:abc"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:99999"}, HTTPS),
        ({"endpoint": 'https://otlp.example.net/a"b'}, HTTPS),
        ({"endpoint": "https://otlp.example.net/a\\b"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/?k=v"}, HTTPS),
        ({"endpoint": "https://otlp.example.net/#f"}, HTTPS),
        ({"endpoint": "https://u:p@[::1"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:65536"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:0"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:00080"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:080"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:"}, HTTPS),
        ({"endpoint": "https://otlp.example.net:123456"}, HTTPS),
        ({"endpoint": "https://OTLP.example.net"}, HTTPS),
        ({"endpoint": "https://otlp.example.net\n"}, HTTPS),
        ({"endpoint": "https://otlp.example.net//"}, HTTPS),
        ({"endpoint": " https://otlp.example.net"}, HTTPS),
        (
            {"endpoint": "https://other.example.net"},
            "host must be listed in [agent.sandbox] network_domains",
        ),
        ({"endpoint": 1}, NON_BLANK),
        ({}, "is required"),
    ],
)
def test_otel_endpoint_refuses_invalid_values(table: Table, reason: str) -> None:
    with pytest.raises(Invalid) as caught:
        config.parse(with_otel(table))
    assert str(caught.value) == f"[agent.otel] endpoint {reason}"


def test_otel_endpoint_error_never_quotes_the_credential() -> None:
    with pytest.raises(Invalid) as caught:
        config.parse(with_otel({"endpoint": "https://user:hunter2@[::1"}))
    assert "hunter2" not in str(caught.value)


def test_otel_endpoint_needs_a_network_domain() -> None:
    with pytest.raises(Invalid) as caught:
        config.parse(GOOD | {"otel": {"endpoint": "https://otlp.example.net"}})
    assert str(caught.value) == (
        "[agent.otel] endpoint host must be listed in [agent.sandbox] network_domains"
    )


def test_otel_refuses_other_keys() -> None:
    with pytest.raises(Invalid) as caught:
        config.parse(with_otel({"endpoint": "https://otlp.example.net", "x": 1}))
    assert str(caught.value) == "[agent.otel] x not recognized"


@pytest.mark.parametrize("entries", [None, "a/b", [1], [""], ["../b"]])
def test_public_repositories_refuse_invalid_entries(entries: object) -> None:
    with pytest.raises(Invalid) as caught:
        config.parse(GOOD | {"public_repos": entries})
    reason = (
        "must contain valid repository arguments" if entries == ["../b"] else STRINGS
    )
    assert str(caught.value) == f"[agent] public_repos {reason}"


UNSAFE_REFERENCES = [
    "a b/c/d",
    "a/b c/d",
    "a/b/c d",
    "a/b/c\t",
    "a/b/c\n",
    "a/b?x/c",
    "a/b/c?x",
    "?a/b/c",
    "a=1/b/c",
    "a/b/c=1",
    "a/b/c\x01",
    "a/\x7fb/c",
    "a/b/c\x00",
    "/b/c",
    "a/b/",
    "-a/b/c",
    "a/.b/c",
    "a/b/_c",
    "a/b/é",
    "a/b/c\uff11",
]

GOOD: Table = {
    "launch_wait_seconds": 120,
    "agy_timeout": "900s",
    "kill_grace_seconds": 10,
    "max_run_minutes": 120,
    "sockets_dir": "/example/sockets",
    "sandbox": {
        "write_roots": [".cache", ".local/share/mise", ".local/state"],
        "audit_dir": ".local/state/example-audit",
        "denied_home_paths": [".claude"],
        "path": {
            "linux": [".local/bin", "/usr/local/bin", "/usr/bin"],
            "macos": [".local/bin", "/opt/homebrew/bin", "/usr/bin"],
        },
        "agy": {
            "review_read": [".config/example-gh", ".config/mise"],
            "build_read": [".config/example-tools"],
            "build_write": [".local/state/mise", ".local/share/mise"],
        },
    },
    "codex": {"web_search": {"build": "cached", "review": "disabled"}},
    "credentials": {
        "agent": {
            "gh": "example-vault/example-gh/token",
            "docker": "example-vault/example-docker/config",
            "reviews_app_id": "example-vault/example-app/app_id",
            "reviews_app_client_id": "example-vault/example-app/client_id",
            "reviews_app_pem": "example-vault/example-app/private_key",
            "signing": "example-vault/example-signing/private_key",
        }
    },
    "claude": {
        "default_profile": "primary",
        "profiles": {
            "primary": {"token": "example/primary/token"},
            "secondary": {
                "token": "example/secondary/token",
                "expires": "example/secondary/expires",
            },
        },
    },
    "host": [
        {
            "name": "example-linux",
            "os": "linux",
            "shell": "bash",
            "account": "agent",
            "unsandboxed": False,
            "accounts": ["agent"],
            "proven_harnesses": ["codex"],
        }
    ],
}

EXPECTED = model.Settings(
    launch_wait_seconds=120,
    agy_timeout_seconds=900,
    kill_grace_seconds=10,
    max_run_minutes=120,
    sockets_dir="/example/sockets",
    sandbox=model.Sandbox(
        write_roots=(".cache", ".local/share/mise", ".local/state"),
        audit_dir=".local/state/example-audit",
        denied_home_paths=(".claude",),
        path=model.SandboxPath(
            linux=(".local/bin", "/usr/local/bin", "/usr/bin"),
            macos=(".local/bin", "/opt/homebrew/bin", "/usr/bin"),
        ),
        agy=model.AgySandbox(
            review_read=(".config/example-gh", ".config/mise"),
            build_read=(".config/example-tools",),
            build_write=(".local/state/mise", ".local/share/mise"),
        ),
    ),
    codex=model.Codex(model.WebSearch.CACHED, model.WebSearch.DISABLED),
    credentials=(
        model.Credentials(
            account="agent",
            gh="example-vault/example-gh/token",
            docker="example-vault/example-docker/config",
            reviews_app_id="example-vault/example-app/app_id",
            reviews_app_client_id="example-vault/example-app/client_id",
            reviews_app_pem="example-vault/example-app/private_key",
            signing="example-vault/example-signing/private_key",
        ),
    ),
    claude=model.Claude(
        default_profile="primary",
        profiles=(
            model.ClaudeProfile("primary", "example/primary/token", None),
            model.ClaudeProfile(
                "secondary", "example/secondary/token", "example/secondary/expires"
            ),
        ),
    ),
    hosts=(
        model.Host(  # noqa: S604 -- a config key, not a subprocess flag
            name="example-linux",
            os=model.Os.LINUX,
            shell="bash",
            account="agent",
            unsandboxed=False,
            accounts=("agent",),
            proven_harnesses=(model.Harness.CODEX,),
        ),
    ),
)


def edited(*path: str | int, value: object = MISSING) -> Table:
    """GOOD with the value at PATH replaced, or removed when VALUE is MISSING."""
    table = copy.deepcopy(GOOD)
    node: object = table
    for step in path[:-1]:
        node = cast("dict[str | int, object]", node)[step]
    holder = cast("dict[str | int, object]", node)
    if value is MISSING:
        del holder[path[-1]]
    else:
        holder[path[-1]] = value
    return table


def refused(table: Table) -> str:
    with pytest.raises(Invalid) as caught:
        config.parse(table)
    return str(caught.value)


def test_parses_every_key() -> None:
    assert config.parse(GOOD) == EXPECTED


def test_host_tag_is_ignored() -> None:
    assert config.parse(edited("host_tag", value="example-tag")) == EXPECTED


def test_settings_expose_every_value() -> None:
    parsed = config.parse(GOOD)
    sandbox = parsed.sandbox
    assert sandbox.write_roots == (".cache", ".local/share/mise", ".local/state")
    assert sandbox.audit_dir == ".local/state/example-audit"
    assert sandbox.denied_home_paths == (".claude",)
    assert sandbox.agy.review_read == (".config/example-gh", ".config/mise")
    assert sandbox.agy.build_read == (".config/example-tools",)
    assert sandbox.agy.build_write == (".local/state/mise", ".local/share/mise")
    assert parsed.codex.web_search_build is model.WebSearch.CACHED
    assert parsed.codex.web_search_review is model.WebSearch.DISABLED
    account = parsed.credentials[0]
    assert account.reviews_app_id == "example-vault/example-app/app_id"
    assert account.reviews_app_client_id == "example-vault/example-app/client_id"
    assert account.signing == "example-vault/example-signing/private_key"
    assert parsed.sockets_dir == "/example/sockets"


def test_input_is_not_modified() -> None:
    table = copy.deepcopy(GOOD)
    config.parse(table)
    assert table == GOOD


@pytest.mark.parametrize(
    "path", ["/", "/etc", ".", "./", "././", "~", "~/x", "~user/x"]
)
def test_sandbox_denied_paths_stay_below_home(path: str) -> None:
    table = edited("sandbox", "denied_home_paths", value=["safe", path])
    assert refused(table) == (
        "[agent.sandbox] denied_home_paths must be paths below home "
        "without a leading '/' or '~'"
    )


@pytest.mark.parametrize("path", ["/example/host/temp", "/example/host/cache"])
def test_sandbox_write_roots_accept_absolute_entries(path: str) -> None:
    parsed = config.parse(edited("sandbox", "write_roots", value=["safe", path]))
    assert parsed.sandbox.write_roots == ("safe", path)


@pytest.mark.parametrize(
    "path", ["/", "//", "///", "/./", ".", "./", "././", "~", "~/x", "~user/x"]
)
def test_sandbox_write_roots_reject_roots_empty_and_tilde_paths(path: str) -> None:
    table = edited("sandbox", "write_roots", value=["safe", path])
    assert refused(table) == (
        "[agent.sandbox] write_roots must be paths without a leading '~', "
        "an empty path or a root"
    )


@pytest.mark.parametrize("path", ["/example/../temp", "/example/\x00temp"])
def test_sandbox_write_roots_reject_unsafe_absolute_entries(path: str) -> None:
    table = edited("sandbox", "write_roots", value=["safe", path])
    assert refused(table) == f"[agent.sandbox] write_roots {PATHS}"


@pytest.mark.parametrize("key", ["write_roots", "denied_home_paths"])
@pytest.mark.parametrize("path", ["x", "./x", ".hidden", "x~", "x/y"])
def test_sandbox_home_paths_keep_relative_entries(key: str, path: str) -> None:
    parsed = config.parse(edited("sandbox", key, value=[path]))
    assert getattr(parsed.sandbox, key) == (path,)


@pytest.mark.parametrize("os", ["linux", "macos"])
def test_search_path_still_accepts_absolute_entries(os: str) -> None:
    parsed = config.parse(edited("sandbox", "path", os, value=["/usr/bin"]))
    assert getattr(parsed.sandbox.path, os) == ("/usr/bin",)


def test_result_is_frozen() -> None:
    assert hash(config.parse(GOOD)) == hash(EXPECTED)


def test_optional_keys_may_be_absent() -> None:
    table = edited("max_run_minutes")
    del table["codex"]
    del cast(
        "Table", cast("Table", cast("Table", table["claude"])["profiles"])["secondary"]
    )["expires"]
    del cast("Table", cast("Table", table["credentials"])["agent"])["docker"]
    parsed = config.parse(table)
    assert parsed.max_run_minutes is None
    assert parsed.codex == model.Codex(model.WebSearch.LIVE, model.WebSearch.LIVE)
    assert parsed.claude.profiles[1].expires is None
    assert parsed.credentials[0].docker is None


@pytest.mark.parametrize(
    ("codex", "build", "review"),
    [
        ({}, "live", "live"),
        ({"web_search": {}}, "live", "live"),
        ({"web_search": {"build": "disabled"}}, "disabled", "live"),
        ({"web_search": {"review": "cached"}}, "live", "cached"),
        ({"web_search": {"build": "live", "review": "live"}}, "live", "live"),
        ({"web_search": {"build": "cached", "review": "cached"}}, "cached", "cached"),
    ],
)
def test_codex_web_search_defaults_to_live_and_may_narrow(
    codex: Table, build: str, review: str
) -> None:
    parsed = config.parse(edited("codex", value=codex))
    assert parsed.codex == model.Codex(model.WebSearch(build), model.WebSearch(review))


def test_minimal_values_are_valid() -> None:
    table = edited("launch_wait_seconds", value=1)
    table["kill_grace_seconds"] = 1
    table["max_run_minutes"] = 1
    table["agy_timeout"] = 1
    parsed = config.parse(table)
    assert (parsed.launch_wait_seconds, parsed.kill_grace_seconds) == (1, 1)
    assert (parsed.max_run_minutes, parsed.agy_timeout_seconds) == (1, 1)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (900, 900),
        (1, 1),
        ("900", 900),
        ("900s", 900),
        ("1s", 1),
        ("15m", 900),
        ("1h", 3600),
        ("2h", 7200),
        (604800, 604800),
        ("604800", 604800),
        ("604800s", 604800),
        ("10080m", 604800),
        ("168h", 604800),
        (604801, None),
        ("604801", None),
        ("10081m", None),
        ("169h", None),
        ("99999999999999h", None),
        ("007", 7),
        ("0", None),
        ("0s", None),
        ("0h", None),
        (0, None),
        (-1, None),
        (True, None),
        (1.5, None),
        (None, None),
        ([5], None),
        ("", None),
        ("s", None),
        ("-5", None),
        ("5d", None),
        ("5S", None),
        ("1 h", None),
        (" 5", None),
        ("5\n", None),
        ("1h30m", None),
        ("\uff11\uff10", None),
        ("1.5h", None),
    ],
)
def test_parse_duration(value: object, seconds: int | None) -> None:
    assert config.parse_duration(value) == seconds


@pytest.mark.parametrize(
    ("name", "found"),
    [
        ("primary", "primary"),
        ("Primary", "primary"),
        ("PRIMARY", "primary"),
        ("secondary", "secondary"),
        ("tertiary", None),
        ("", None),
    ],
)
def test_find_profile_ignores_case(name: str, found: str | None) -> None:
    profile = config.find_profile(EXPECTED.claude.profiles, name)
    assert (profile.name if profile else None) == found


def test_find_profile_ignores_the_case_config_spells() -> None:
    profiles = (model.ClaudeProfile("Primary", "a/b/c", None),)
    found = config.find_profile(profiles, "primary")
    assert found is not None
    assert found.name == "Primary"


def test_default_profile_resolves_to_the_name_config_spells() -> None:
    table = edited("claude", "default_profile", value="PRIMARY")
    assert config.parse(table).claude.default_profile == "primary"
    claude = cast("Table", table["claude"])
    claude["profiles"] = {
        "Primary": {"token": "e/p/t"},
        "Secondary": {"token": "e/s/t"},
    }
    claude["default_profile"] = "secondary"
    parsed = config.parse(table).claude
    assert parsed.default_profile == "Secondary"
    assert [p.name for p in parsed.profiles] == ["Primary", "Secondary"]


type Case = tuple[tuple[str | int, ...], object, str]


def _add(
    cases: list[Case], path: tuple[str | int, ...], value: object, why: str, label: str
) -> None:
    cases.append((path, value, f"{label} {path[-1]} {why}"))


def _root_errors() -> list[Case]:
    cases: list[Case] = []
    root = "[agent]"
    for key in ("launch_wait_seconds", "kill_grace_seconds", "max_run_minutes"):
        for bad in (0, -1, True, "5", 1.5, None, [5]):
            _add(cases, (key,), bad, POSITIVE, root)
    for bad in (0, "0", "0s", "abc", "", "5d", True, 1.5, [5], None, 604801, "169h"):
        _add(cases, ("agy_timeout",), bad, DURATION, root)
    for key in ("launch_wait_seconds", "agy_timeout", "kill_grace_seconds"):
        _add(cases, (key,), MISSING, "is required", root)
    _add(cases, ("sockets_dir",), MISSING, "is required", root)
    for bad in ("", "  ", 5, None, ["x"]):
        _add(cases, ("sockets_dir",), bad, NON_BLANK, root)
    for bad in ("/a/../b", "a\x00b", ".."):
        _add(cases, ("sockets_dir",), bad, PATHS, root)
    for key in ("sandbox", "credentials", "claude", "host"):
        _add(cases, (key,), MISSING, "is required", root)
    for key in ("sandbox", "credentials", "claude", "codex"):
        _add(cases, (key,), 5, "must be a table", root)
    return cases


def _sandbox_errors() -> list[Case]:
    cases: list[Case] = []
    sandbox = "[agent.sandbox]"
    for key in ("path", "agy"):
        _add(cases, ("sandbox", key), MISSING, "is required", sandbox)
        _add(cases, ("sandbox", key), [], "must be a table", sandbox)
    _add(cases, ("sandbox", "audit_dir"), MISSING, "is required", sandbox)
    for bad in ("", " ", 5):
        _add(cases, ("sandbox", "audit_dir"), bad, NON_BLANK, sandbox)
    for bad in ("../x", "a/../b", "a\x00"):
        _add(cases, ("sandbox", "audit_dir"), bad, PATHS, sandbox)
    lists = (
        ("sandbox", "write_roots", sandbox),
        ("sandbox", "denied_home_paths", sandbox),
        ("sandbox", "path", "linux", "[agent.sandbox.path]"),
        ("sandbox", "path", "macos", "[agent.sandbox.path]"),
        ("sandbox", "agy", "review_read", "[agent.sandbox.agy]"),
        ("sandbox", "agy", "build_read", "[agent.sandbox.agy]"),
        ("sandbox", "agy", "build_write", "[agent.sandbox.agy]"),
    )
    for *path, label in lists:
        where = tuple(path)
        _add(cases, where, MISSING, "is required", label)
        for bad in (EMPTY, [""], [" "], [5], "text", ["ok", None], None):
            _add(cases, where, bad, STRINGS, label)
        for bad in (["../x"], ["ok", "a/../b"], ["a\x00"], [".."]):
            _add(cases, where, bad, PATHS, label)
    return cases


def _codex_errors() -> list[Case]:
    cases: list[Case] = []
    codex = "[agent.codex]"
    _add(cases, ("codex", "web_search"), 5, "must be a table", codex)
    for bad in ("bad", "Live", 5, True, None, ["live"], ""):
        _add(
            cases,
            ("codex", "web_search", "build"),
            bad,
            "must be one of live, cached, disabled",
            "[agent.codex.web_search]",
        )
        _add(
            cases,
            ("codex", "web_search", "review"),
            bad,
            "must be one of live, cached, disabled",
            "[agent.codex.web_search]",
        )
    return cases


def _credential_errors() -> list[Case]:
    cases: list[Case] = []
    account = "[agent.credentials.agent]"
    for key in REFERENCE_KEYS:
        if key in ("gh", "docker"):
            continue
        _add(cases, ("credentials", "agent", key), MISSING, "is required", account)
    for key in REFERENCE_KEYS:
        for bad in ("a/b", "a/b/c/d", "a//c", "a/ /c", "", 5, None, "op://a/b/c"):
            _add(cases, ("credentials", "agent", key), bad, REFERENCE, account)
        for bad in UNSAFE_REFERENCES:
            if key == "signing" and bad == "/b/c":
                continue
            _add(cases, ("credentials", "agent", key), bad, REFERENCE, account)
    return cases


def _claude_errors() -> list[Case]:
    cases: list[Case] = []
    claude = "[agent.claude]"
    _add(cases, ("claude", "default_profile"), MISSING, "is required", claude)
    for bad in ("", " ", 5):
        _add(cases, ("claude", "default_profile"), bad, NON_BLANK, claude)
    _add(cases, ("claude", "profiles"), MISSING, "is required", claude)
    _add(cases, ("claude", "profiles"), "x", "must be a table", claude)
    for key in ("token",):
        _add(
            cases,
            ("claude", "profiles", "primary", key),
            MISSING,
            "is required",
            "[agent.claude.profiles.primary]",
        )
    for key in ("token", "expires"):
        for bad in ("a/b", "", 5, "a/b/c/d", *UNSAFE_REFERENCES):
            _add(
                cases,
                ("claude", "profiles", "primary", key),
                bad,
                REFERENCE,
                "[agent.claude.profiles.primary]",
            )
    _add(
        cases,
        ("claude", "profiles", "primary"),
        5,
        "must be a table",
        "[agent.claude.profiles]",
    )
    return cases


def _host_errors() -> list[Case]:
    cases: list[Case] = []
    host = "[[agent.host]]"
    for key in ("name", "os", "shell", "account", "unsandboxed", "accounts"):
        _add(cases, ("host", 0, key), MISSING, "is required", host)
    for bad in ("", " ", 5):
        _add(cases, ("host", 0, "name"), bad, NON_BLANK, host)
        _add(cases, ("host", 0, "shell"), bad, NON_BLANK, host)
        _add(cases, ("host", 0, "account"), bad, NON_BLANK, host)
    names = (
        "-x",
        "a b",
        "a" * 64,
        ".a",
        "a.",
        "a..b",
        "a.-b",
        "a-.b",
        "a.b-",
        "a/b",
        "é",
        "A",
        "a_b",
        "a.b\n",
    )
    for bad in names:
        _add(cases, ("host", 0, "name"), bad, f"must match {HOST_RULE}", host)
    for bad in ("windows", "Linux", "", 5, None, ["linux"]):
        _add(cases, ("host", 0, "os"), bad, "must be one of linux, macos", host)
    for bad in ("Bash", "sh", "fish", "a" * 33, "a b", "bash\n", "bash ", "zsh5"):
        _add(cases, ("host", 0, "shell"), bad, "must be one of bash, zsh", host)
    for bad in ("-x", "a b", "a" * 65, "a.b"):
        _add(cases, ("host", 0, "account"), bad, f"must match {NAME_RULE}", host)
    for bad in ("yes", 1, 0, None, "true"):
        _add(cases, ("host", 0, "unsandboxed"), bad, "must be true or false", host)
    for bad in (EMPTY, [""], [5], "agent", None):
        _add(cases, ("host", 0, "accounts"), bad, STRINGS, host)
    for bad in (["a b"], ["agent", "-x"], ["a" * 65]):
        _add(
            cases,
            ("host", 0, "accounts"),
            bad,
            f"must list names matching {NAME_RULE}",
            host,
        )
    _add(
        cases,
        ("host", 0, "accounts"),
        ["agent", "agent"],
        "must not contain duplicates",
        host,
    )
    cases.append(
        (("host", 0, "accounts"), ["ops"], f"{host} account must be one of accounts")
    )
    cases.extend(
        (
            ("host", 0, "accounts"),
            accounts,
            f"{host} accounts names ops, which has no [agent.credentials.ops]",
        )
        for accounts in (["agent", "ops"], ["agent", "ops", "other"])
    )
    return cases


def _errors() -> list[Case]:
    return [
        *_root_errors(),
        *_sandbox_errors(),
        *_codex_errors(),
        *_credential_errors(),
        *_claude_errors(),
        *_host_errors(),
    ]


@pytest.mark.parametrize(("path", "value", "message"), _errors())
def test_refuses(path: tuple[str | int, ...], value: object, message: str) -> None:
    assert refused(edited(*path, value=value)) == message


def test_one_valid_account_name_per_table_key() -> None:
    table = edited("credentials", value={"bad name": GOOD_ACCOUNT})
    assert refused(table) == f"[agent.credentials] bad name must match {NAME_RULE}"


GOOD_ACCOUNT = cast("Table", cast("Table", GOOD["credentials"])["agent"])


def test_credentials_need_an_account() -> None:
    assert refused(edited("credentials", value={})) == (
        "[agent.credentials] account is required"
    )


def test_credentials_account_must_be_a_table() -> None:
    assert refused(edited("credentials", value={"agent": 5})) == (
        "[agent.credentials] agent must be a table"
    )


def test_each_account_has_its_own_table_label() -> None:
    table = edited("credentials", value={"agent": GOOD_ACCOUNT, "other": {}})
    assert refused(table) == "[agent.credentials.other] signing is required"
    table = edited("credentials", value={"agent": GOOD_ACCOUNT, "other": GOOD_ACCOUNT})
    assert [c.account for c in config.parse(table).credentials] == ["agent", "other"]


def test_profiles_need_one() -> None:
    assert refused(edited("claude", "profiles", value={})) == (
        "[agent.claude.profiles] profile is required"
    )


@pytest.mark.parametrize("name", ["-x", "a b", "a" * 65, "a.b", ""])
def test_profile_names_must_match_the_name_rule(name: str) -> None:
    table = edited("claude", "profiles", value={name: {"token": "a/b/c"}})
    assert refused(table) == f"[agent.claude.profiles] {name} must match {NAME_RULE}"


@pytest.mark.parametrize(
    ("first", "second"),
    [("Primary", "primary"), ("primary", "PRIMARY"), ("aB", "Ab")],
)
def test_profiles_differing_only_in_case_fail(first: str, second: str) -> None:
    profiles = {first: {"token": "a/b/c"}, second: {"token": "a/b/d"}}
    table = edited("claude", "profiles", value=profiles)
    assert refused(table) == (
        f"[agent.claude.profiles] {second} differs from {first} only in case"
    )


def test_default_profile_must_name_a_profile() -> None:
    table = edited("claude", "default_profile", value="tertiary")
    assert refused(table) == (
        "[agent.claude] default_profile names no profile in [agent.claude.profiles]"
    )


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ((), "[agent]"),
        (("sandbox",), "[agent.sandbox]"),
        (("sandbox", "path"), "[agent.sandbox.path]"),
        (("sandbox", "agy"), "[agent.sandbox.agy]"),
        (("codex",), "[agent.codex]"),
        (("codex", "web_search"), "[agent.codex.web_search]"),
        (("credentials", "agent"), "[agent.credentials.agent]"),
        (("claude",), "[agent.claude]"),
        (("claude", "profiles", "primary"), "[agent.claude.profiles.primary]"),
        (("host", 0), "[[agent.host]]"),
    ],
)
def test_unknown_keys_name_the_full_table_and_each_key(
    path: tuple[str | int, ...], label: str
) -> None:
    table = copy.deepcopy(GOOD)
    node: object = table
    for step in path:
        node = cast("dict[str | int, object]", node)[step]
    cast("Table", node).update({"zeta": 1, "alpha": 2})
    assert refused(table) == f"{label} alpha, zeta not recognized"


def test_credentials_is_not_a_host_key() -> None:
    table = edited("host", 0, "credentials", value="connect")
    assert refused(table) == "[[agent.host]] credentials not recognized"


@pytest.mark.parametrize("names", [[], ["codex"], ["claude"], ["codex", "claude"]])
def test_host_records_proven_harnesses(names: list[str]) -> None:
    table = edited("host", 0, "proven_harnesses", value=names)
    assert config.parse(table).hosts[0].proven_harnesses == tuple(
        model.Harness(name) for name in names
    )


@pytest.mark.parametrize(
    "value", [None, "codex", [1], ["agy"], ["Codex"], ["codex", "codex"]]
)
def test_host_refuses_invalid_proven_harnesses(value: object) -> None:
    assert refused(edited("host", 0, "proven_harnesses", value=value)) == (
        "[[agent.host]] proven_harnesses must list distinct codex or claude names"
    )


@pytest.mark.parametrize("value", [[], 5, "x", [5], ["x"], [None]])
def test_host_must_be_a_non_empty_array_of_tables(value: object) -> None:
    assert refused(edited("host", value=value)) == (
        "[agent] host must be a non-empty array of tables"
    )


def test_hosts_keep_their_order_and_refuse_a_repeated_name() -> None:
    second = {**cast("Table", cast("list[object]", GOOD["host"])[0]), "name": "other"}
    first = cast("list[object]", GOOD["host"])[0]
    table = edited("host", value=[first, second])
    assert [h.name for h in config.parse(table).hosts] == ["example-linux", "other"]
    table = edited("host", value=[first, first])
    assert refused(table) == "[[agent.host]] name 'example-linux' is defined twice"


def test_a_host_may_run_on_macos_with_a_dotted_name() -> None:
    first = cast("Table", cast("list[object]", GOOD["host"])[0])
    table = edited(
        "host",
        value=[{**first, "name": "example.mac-1", "os": "macos", "shell": "zsh"}],
    )
    host = config.parse(table).hosts[0]
    assert (host.name, host.os, host.shell) == (
        "example.mac-1",
        model.Os.MACOS,
        "zsh",
    )
    assert host.unsandboxed is False


def test_name_limits() -> None:
    first = cast("Table", cast("list[object]", GOOD["host"])[0])
    table = edited(
        "host",
        value=[
            {**first, "name": "a" * 63, "account": "b" * 64, "accounts": ["b" * 64]}
        ],
    )
    table["credentials"] = {"b" * 64: GOOD_ACCOUNT}
    host = config.parse(table).hosts[0]
    assert (len(host.name), len(host.account)) == (63, 64)


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_a_host_runs_bash_or_zsh(shell: str) -> None:
    assert config.parse(edited("host", 0, "shell", value=shell)).hosts[0].shell == shell


@pytest.mark.parametrize(
    "name",
    [
        "a",
        "0",
        "a-b",
        "a.b",
        "a.b.c",
        "example.tail-1234.ts.net",
        "a-b.c-d",
        "9.9",
        "a" * 63,
        f"{'a' * 30}.{'b' * 32}",
    ],
)
def test_host_names_are_dot_separated_labels(name: str) -> None:
    table = edited("host", 0, "name", value=name)
    assert config.parse(table).hosts[0].name == name


def test_a_host_may_list_accounts_that_have_credentials() -> None:
    table = edited("host", 0, "accounts", value=["agent", "ops"])
    table["credentials"] = {"agent": GOOD_ACCOUNT, "ops": GOOD_ACCOUNT}
    assert config.parse(table).hosts[0].accounts == ("agent", "ops")


def test_every_host_account_needs_credentials() -> None:
    first = cast("Table", cast("list[object]", GOOD["host"])[0])
    second = {**first, "name": "other", "account": "ops", "accounts": ["ops", "more"]}
    table = edited("host", value=[first, second])
    assert refused(table) == (
        "[[agent.host]] accounts names ops, which has no [agent.credentials.ops]"
    )
    table["credentials"] = {"agent": GOOD_ACCOUNT, "ops": GOOD_ACCOUNT}
    assert refused(table) == (
        "[[agent.host]] accounts names more, which has no [agent.credentials.more]"
    )


def test_reference_parts_may_hold_dots_underscores_and_hyphens() -> None:
    reference = "v-1/i.t_2/F-3.x"
    table = edited("claude", "profiles", "primary", "token", value=reference)
    assert config.parse(table).claude.profiles[0].token == reference


@pytest.mark.parametrize(
    ("key", "value", "valid"),
    [
        ("host_path", ["/opt/example/bin"], True),
        ("host_path", ["relative"], False),
        ("host_path", ["/"], False),
        ("host_path", ["/opt/../bin"], False),
        ("host_path", [], False),
        ("network_domains", [], True),
        ("network_domains", ["api.github.com"], True),
        ("network_domains", ["*.github.com"], False),
        ("network_domains", ["https://github.com"], False),
        ("network_domains", ["github.com:443"], False),
        ("network_domains", [""], False),
        ("network_domains", "github.com", False),
        ("codex_network_proxy_opt_out", "named reason", True),
        ("codex_network_proxy_opt_out", "", False),
        ("agy_unsandboxed_opt_out", "named reason", True),
        ("codex_unsandboxed_opt_out", "isolated host", True),
        ("codex_unsandboxed_opt_out", "", False),
        ("codex_unsandboxed_opt_out", False, False),
        ("agy_unsandboxed_opt_out", False, False),
    ],
)
def test_host_and_network_options(key: str, value: object, valid: bool) -> None:
    data = copy.deepcopy(GOOD)
    sandbox = cast("Table", data["sandbox"])
    sandbox[key] = value
    if valid:
        result = config.parse(data)
        assert getattr(result.sandbox, key) == (
            tuple(value) if isinstance(value, list) else value
        )
    else:
        with pytest.raises(Invalid, match=key):
            config.parse(data)


@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        ("host_path", ["relative"], "must contain absolute paths only"),
        (
            "network_domains",
            ["*.example.com"],
            "must contain exact host names without wildcards",
        ),
    ],
)
def test_execution_options_name_the_invalid_path(
    key: str, value: object, why: str
) -> None:
    data = copy.deepcopy(GOOD)
    cast("Table", data["sandbox"])[key] = value
    with pytest.raises(Invalid) as caught:
        config.parse(data)
    assert str(caught.value) == f"[agent.sandbox] {key} {why}"


def test_unsandboxed_host_refuses_reviews_app_delivery() -> None:
    assert refused(edited("host", 0, "unsandboxed", value=True)) == (
        "[[agent.host]] unsandboxed host example-linux "
        "has reviews App credentials for agent"
    )


def test_unsandboxed_host_without_reviews_app_is_allowed() -> None:
    table = edited("host", 0, "unsandboxed", value=True)
    accounts = cast("Table", table["credentials"])
    account = cast("Table", accounts["agent"])
    for key in ("reviews_app_id", "reviews_app_client_id", "reviews_app_pem"):
        del account[key]
    parsed = config.parse(table)
    assert parsed.hosts[0].unsandboxed is True
    assert parsed.credentials[0].reviews_app_id is None
    assert parsed.credentials[0].reviews_app_client_id is None
    assert parsed.credentials[0].reviews_app_pem is None


@pytest.mark.parametrize(
    "present", ["reviews_app_id", "reviews_app_client_id", "reviews_app_pem"]
)
def test_partial_reviews_app_configuration_is_refused(present: str) -> None:
    table = edited("host", 0, "unsandboxed", value=True)
    accounts = cast("Table", table["credentials"])
    account = cast("Table", accounts["agent"])
    for key in ("reviews_app_id", "reviews_app_client_id", "reviews_app_pem"):
        if key != present:
            del account[key]
    missing = (
        "reviews_app_client_id" if present == "reviews_app_id" else "reviews_app_id"
    )
    assert refused(table) == f"[agent.credentials.agent] {missing} is required"


@pytest.mark.parametrize("item", ["item", "abcdefghijklmnopqrstuvwxyz"])
@pytest.mark.parametrize("label", ["private key", "private_key", "k", "private  key"])
def test_signing_accepts_openssh_reference(item: str, label: str) -> None:
    reference = f"vault/{item}/{label}?ssh-format=openssh"
    table = edited("credentials", "agent", "signing", value=reference)
    assert config.parse(table).credentials[0].signing == reference


@pytest.mark.parametrize(
    "field",
    [
        "private key?ssh-format=pkcs8",
        "private key?attribute=otp",
        "private key?ssh-format=openssh&x=1",
        "private key?",
        " private key?ssh-format=openssh",
        "private key ?ssh-format=openssh",
        "private/key?ssh-format=openssh",
        "..?ssh-format=openssh",
        "private..key?ssh-format=openssh",
        "private\tkey?ssh-format=openssh",
        "private\nkey?ssh-format=openssh",
        "private\x00key?ssh-format=openssh",
        "private\x7fkey?ssh-format=openssh",
        "private key?ssh-format=openssh?ssh-format=openssh",
    ],
)
def test_signing_refuses_other_extensions(field: str) -> None:
    table = edited("credentials", "agent", "signing", value=f"vault/item/{field}")
    assert REFERENCE in refused(table)


@pytest.mark.parametrize("key", [k for k in REFERENCE_KEYS if k != "signing"])
@pytest.mark.parametrize("field", ["key", "private key"])
def test_other_credentials_refuse_openssh_query(key: str, field: str) -> None:
    table = edited(
        "credentials", "agent", key, value=f"vault/item/{field}?ssh-format=openssh"
    )
    assert REFERENCE in refused(table)


@pytest.mark.parametrize("key", ["token", "expires"])
def test_claude_references_refuse_openssh_query(key: str) -> None:
    table = edited(
        "claude",
        "profiles",
        "primary",
        key,
        value="vault/item/key?ssh-format=openssh",
    )
    assert REFERENCE in refused(table)


def test_github_credential_is_optional() -> None:
    parsed = config.parse(edited("credentials", "agent", "gh"))
    assert parsed.credentials[0].gh is None


@pytest.mark.parametrize("value", ["/example/key", "/key with spaces"])
def test_signing_accepts_absolute_key_path(value: str) -> None:
    table = edited("credentials", "agent", "signing", value=value)
    assert config.parse(table).credentials[0].signing == value


@pytest.mark.parametrize("value", ["./key", "key", "../key", "/a/../key", "/key\x00"])
def test_signing_refuses_unsafe_key_path(value: str) -> None:
    table = edited("credentials", "agent", "signing", value=value)
    assert (PATHS if value.startswith("/") else REFERENCE) in refused(table)


@pytest.mark.parametrize(
    ("key", "default"),
    [
        ("ssh_connect_timeout", 15),
        ("ssh_keepalive_seconds", 30),
        ("ssh_keepalive_count_max", 3),
    ],
)
def test_ssh_settings(key: str, default: int) -> None:
    assert getattr(config.parse(GOOD), key) == default
    assert getattr(config.parse(edited(key, value=9)), key) == 9
    for value in (0, -1, True, "9"):
        assert (
            refused(edited(key, value=value))
            == f"[agent] {key} must be a positive integer"
        )


@pytest.mark.parametrize(
    "program", [None, "/example/bin/tbhbdev", "relative", "/a/../b", "/a\x00b"]
)
def test_host_tbhbdev_path(program: str | None) -> None:
    value = copy.deepcopy(GOOD)
    hosts = cast("list[Table]", value["host"])
    if program is not None:
        hosts[0]["tbhbdev"] = program
    if program in (None, "/example/bin/tbhbdev"):
        assert config.parse(value).hosts[0].tbhbdev == program
    else:
        with pytest.raises(Invalid) as caught:
            config.parse(value)
        why = "must be an absolute path" if program == "relative" else PATHS
        assert str(caught.value) == f"[[agent.host]] tbhbdev {why}"
