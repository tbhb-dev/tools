"""Parsing the user config's `[agent]` table into `Settings`.

The table arrives already parsed, as `userconfig.table` returns it. Parsing has
defaults for connection bounds, Codex's `web_search` and the host executable.
Errors name
the key, as `[agent.sandbox] audit_dir must be ...`. The caller adds the config
file's path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import NoReturn

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import (
    DEFAULT_CODEX_SUBAGENT_MODEL,
    DEFAULT_MAX_CONCURRENT_RUNS,
    DEFAULT_RUN_MEMORY_MIB,
    HOST_NAME,
    NAME,
    AgySandbox,
    Claude,
    ClaudeProfile,
    Codex,
    Credentials,
    Harness,
    Host,
    Invalid,
    Os,
    Sandbox,
    SandboxPath,
    Settings,
    WebSearch,
)

_DURATION = re.compile(r"([0-9]+)([smh]?)")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_MAX_SECONDS = 7 * 24 * 3600
_REFERENCE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SIGNING_REFERENCE = re.compile(
    rf"{_REFERENCE_PART.pattern}/{_REFERENCE_PART.pattern}/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?\?ssh-format=openssh"
)
_REFERENCE_PARTS = 3
_PROGRAMS = ("bash", "zsh")
_MAX_PERCENT = 100


def parse_duration(value: object) -> int | None:
    """The seconds in VALUE, or None when it isn't a duration of 1 s to 7 days.

    VALUE is bare seconds, as an integer or a string, or a string such as
    `900s`, `15m` or `1h`.
    """
    if type(value) is int:
        return value if 0 < value <= _MAX_SECONDS else None
    if not isinstance(value, str):
        return None
    found = _DURATION.fullmatch(value)
    if found is None:
        return None
    seconds = int(found[1]) * _UNIT_SECONDS[found[2]]
    return seconds if 0 < seconds <= _MAX_SECONDS else None


def find_profile(
    profiles: tuple[ClaudeProfile, ...], name: str
) -> ClaudeProfile | None:
    """The profile NAME selects, matching without regard to case."""
    return next((p for p in profiles if p.name.lower() == name.lower()), None)


@dataclass(frozen=True)
class _Table:
    """One config table and its label, as `[agent.sandbox]`."""

    label: str
    values: dict[str, object]

    def fail(self, key: str, why: str) -> NoReturn:
        message = f"{self.label} {key} {why}"
        raise Invalid(message)

    def only(self, *keys: str) -> None:
        unknown = self.values.keys() - set(keys)
        if unknown:
            self.fail(", ".join(sorted(unknown)), "not recognized")

    def value(self, key: str) -> object:
        if key not in self.values:
            self.fail(key, "is required")
        return self.values[key]

    def table(self, key: str) -> _Table:
        value = self.value(key)
        if not isinstance(value, dict):
            self.fail(key, "must be a table")
        values: dict[str, object] = value
        return _Table(f"{self.label.removesuffix(']')}.{key}]", values)

    def text(self, key: str) -> str:
        value = self.value(key)
        if not isinstance(value, str) or not value.strip():
            self.fail(key, "must be a non-blank string")
        return value

    def path(self, key: str) -> str:
        value = self.text(key)
        if _bad_path(value):
            self.fail(key, "must not contain a NUL or '..' part")
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        value = self.value(key)
        entries: list[object] = value if isinstance(value, list) else []
        if not entries or not all(isinstance(e, str) and e.strip() for e in entries):
            self.fail(key, "must be a non-empty list of non-blank strings")
        return tuple(str(e) for e in entries)

    def paths(self, key: str) -> tuple[str, ...]:
        entries = self.strings(key)
        if any(_bad_path(e) for e in entries):
            self.fail(key, "must not contain a NUL or '..' part")
        return entries

    def home_paths(self, key: str) -> tuple[str, ...]:
        entries = self.paths(key)
        if any(e.startswith(("/", "~")) or not PurePosixPath(e).parts for e in entries):
            self.fail(key, "must be paths below home without a leading '/' or '~'")
        return entries

    def integer(self, key: str) -> int:
        value = self.value(key)
        if type(value) is not int or value <= 0:
            self.fail(key, "must be a positive integer")
        return value

    def nonnegative(self, key: str, default: int) -> int:
        value = self.values.get(key, default)
        if type(value) is not int or value < 0:
            self.fail(key, "must be a non-negative integer")
        return value

    def duration(self, key: str) -> int:
        seconds = parse_duration(self.value(key))
        if seconds is None:
            why = "must be bare seconds or a number ending in s, m or h, up to 7 days"
            self.fail(key, why)
        return seconds

    def flag(self, key: str) -> bool:
        value = self.value(key)
        if not isinstance(value, bool):
            self.fail(key, "must be true or false")
        return value

    def reference(self, key: str) -> str:
        value = self.value(key)
        if key == "signing" and isinstance(value, str) and value.startswith("/"):
            return self.path(key)
        parts = value.split("/") if isinstance(value, str) else []
        if (
            key == "signing"
            and isinstance(value, str)
            and _SIGNING_REFERENCE.fullmatch(value)
            and ".." not in parts[2]
        ):
            return value
        if len(parts) != _REFERENCE_PARTS or not all(
            _REFERENCE_PART.fullmatch(p) for p in parts
        ):
            self.fail(key, "must be a vault/item/field reference")
        return str(value)

    def choice[E: StrEnum](self, key: str, kind: type[E]) -> E:
        value = self.value(key)
        members = {member.value: member for member in kind}
        if not isinstance(value, str) or value not in members:
            self.fail(key, f"must be one of {', '.join(members)}")
        return members[value]


def _bad_path(text: str) -> bool:
    return "\x00" in text or ".." in text.split("/")


def _write_roots(table: _Table, key: str = "write_roots") -> tuple[str, ...]:
    entries = table.paths(key)
    if any(
        e.startswith("~") or PurePosixPath(e).parent == PurePosixPath(e)
        for e in entries
    ):
        table.fail(
            key,
            "must be paths without a leading '~', an empty path or a root",
        )
    return entries


def _optional_reason(table: _Table, key: str) -> str | None:
    return table.text(key) if key in table.values else None


def _host_path(table: _Table) -> tuple[str, ...]:
    if "host_path" not in table.values:
        return ()
    entries = _write_roots(table, "host_path")
    if any(not PurePosixPath(e).is_absolute() for e in entries):
        table.fail("host_path", "must contain absolute paths only")
    return entries


def _network_domains(table: _Table) -> tuple[str, ...]:
    if table.values.get("network_domains") == []:
        return ()
    if "network_domains" not in table.values:
        return ()
    entries = table.strings("network_domains")
    if any(not HOST_NAME.fullmatch(e) for e in entries):
        table.fail("network_domains", "must contain exact host names without wildcards")
    return entries


def _public_repos(table: _Table) -> tuple[str, ...]:
    entries = table.values.get("public_repos", [])
    if entries == []:
        return ()
    values = table.strings("public_repos")
    for value in values:
        try:
            parts = repo.parts(value)
        except Invalid:
            table.fail("public_repos", "must contain valid repository arguments")
        else:
            if parts[1].lower() != repo.DEFAULT_ORG:
                table.fail(
                    "public_repos", f"must contain only {repo.DEFAULT_ORG} repositories"
                )
    return values


_OTEL_SHAPE = (
    "must be an http or https URL with no credentials, path, query or fragment"
)
# Match the whole http or https URL so quotes, control characters and `@`
# can't reach the config file. Parser errors don't quote the value.
_OTEL_URL = re.compile(
    r"https?://(?P<host>[a-z0-9.-]+)(?::(?P<port>[1-9][0-9]{0,4}))?/?"
)
MAX_PORT = 65535


def _otel_endpoint(root: _Table, network_domains: tuple[str, ...]) -> str | None:
    """The base URL a harness exports telemetry to, which carries no credential.

    The URL has no path, so a token can't ride in it, and its host must be one
    the runs may already reach.
    """
    if "otel" not in root.values:
        return None
    table = root.table("otel")
    table.only("endpoint")
    endpoint = table.text("endpoint")
    match = _OTEL_URL.fullmatch(endpoint)
    if match is None:
        table.fail("endpoint", _OTEL_SHAPE)
    if match["port"] and int(match["port"]) > MAX_PORT:
        table.fail("endpoint", _OTEL_SHAPE)
    if match["host"] not in network_domains:
        table.fail("endpoint", "host must be listed in [agent.sandbox] network_domains")
    return endpoint


def _worker_path(root: _Table) -> str | None:
    """The registered worker executable, which has no default."""
    if "worker" not in root.values:
        return None
    table = root.table("worker")
    table.only("path")
    path = table.path("path")
    if not path.startswith("/"):
        table.fail("path", "must be an absolute path")
    return path


def _sandbox(table: _Table) -> Sandbox:
    table.only(
        "write_roots",
        "audit_dir",
        "denied_home_paths",
        "path",
        "agy",
        "host_path",
        "network_domains",
        "codex_network_proxy_opt_out",
        "agy_unsandboxed_opt_out",
        "codex_unsandboxed_opt_out",
    )
    path = table.table("path")
    path.only("linux", "macos")
    agy = table.table("agy")
    agy.only("review_read", "build_read", "build_write")
    return Sandbox(
        write_roots=_write_roots(table),
        host_path=_host_path(table),
        network_domains=_network_domains(table),
        codex_network_proxy_opt_out=_optional_reason(
            table, "codex_network_proxy_opt_out"
        ),
        agy_unsandboxed_opt_out=_optional_reason(table, "agy_unsandboxed_opt_out"),
        codex_unsandboxed_opt_out=_optional_reason(table, "codex_unsandboxed_opt_out"),
        audit_dir=table.path("audit_dir"),
        denied_home_paths=table.home_paths("denied_home_paths"),
        path=SandboxPath(linux=path.paths("linux"), macos=path.paths("macos")),
        agy=AgySandbox(
            review_read=agy.paths("review_read"),
            build_read=agy.paths("build_read"),
            build_write=agy.paths("build_write"),
        ),
    )


def _codex(table: _Table | None) -> Codex:
    if table is None:
        return Codex(WebSearch.LIVE, WebSearch.LIVE)
    table.only("web_search", "subagent_model")
    model = (
        table.text("subagent_model")
        if "subagent_model" in table.values
        else DEFAULT_CODEX_SUBAGENT_MODEL
    )
    if "web_search" not in table.values:
        return Codex(WebSearch.LIVE, WebSearch.LIVE, model)
    search = table.table("web_search")
    search.only("build", "review")
    return Codex(
        web_search_build=_search(search, "build"),
        web_search_review=_search(search, "review"),
        subagent_model=model,
    )


def _search(table: _Table, key: str) -> WebSearch:
    return table.choice(key, WebSearch) if key in table.values else WebSearch.LIVE


def _check_name(table: _Table, key: str, text: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(text):
        table.fail(key, f"must match {pattern.pattern}")
    return text


def _reviews_app(table: _Table) -> tuple[str | None, str | None, str | None]:
    keys = ("reviews_app_id", "reviews_app_client_id", "reviews_app_pem")
    if not table.values.keys() & set(keys):
        return None, None, None
    return (
        table.reference("reviews_app_id"),
        table.reference("reviews_app_client_id"),
        table.reference("reviews_app_pem"),
    )


def _pull_requests_app(table: _Table) -> tuple[str | None, str | None]:
    keys = ("pull_requests_app_client_id", "pull_requests_app_pem")
    if not table.values.keys() & set(keys):
        return None, None
    return table.reference(keys[0]), table.reference(keys[1])


def _credentials(table: _Table) -> tuple[Credentials, ...]:
    if not table.values:
        table.fail("account", "is required")
    accounts: list[Credentials] = []
    for account in table.values:
        entry = table.table(_check_name(table, account, account, NAME))
        entry.only(
            "gh",
            "docker",
            "reviews_app_id",
            "reviews_app_client_id",
            "reviews_app_pem",
            "pull_requests_app_client_id",
            "pull_requests_app_pem",
            "signing",
        )
        app_id, client_id, pem = _reviews_app(entry)
        pull_requests_client_id, pull_requests_pem = _pull_requests_app(entry)
        gh = entry.reference("gh") if "gh" in entry.values else None
        if gh is not None and pull_requests_pem is not None:
            entry.fail("gh", "must not be set with the pull_requests_app keys")
        accounts.append(
            Credentials(
                account=account,
                gh=gh,
                docker=entry.reference("docker") if "docker" in entry.values else None,
                reviews_app_id=app_id,
                reviews_app_client_id=client_id,
                reviews_app_pem=pem,
                signing=entry.reference("signing"),
                pull_requests_app_client_id=pull_requests_client_id,
                pull_requests_app_pem=pull_requests_pem,
            )
        )
    return tuple(accounts)


def _claude(table: _Table) -> Claude:
    table.only("default_profile", "profiles")
    default = table.text("default_profile")
    profiles = table.table("profiles")
    if not profiles.values:
        profiles.fail("profile", "is required")
    found: list[ClaudeProfile] = []
    for name in profiles.values:
        _check_name(profiles, name, name, NAME)
        earlier = next((p.name for p in found if p.name.lower() == name.lower()), None)
        if earlier is not None:
            profiles.fail(name, f"differs from {earlier} only in case")
        entry = profiles.table(name)
        entry.only("token", "expires")
        found.append(
            ClaudeProfile(
                name=name,
                token=entry.reference("token"),
                expires=entry.reference("expires")
                if "expires" in entry.values
                else None,
            )
        )
    selected = find_profile(tuple(found), default)
    if selected is None:
        table.fail("default_profile", f"names no profile in {profiles.label}")
    return Claude(default_profile=selected.name, profiles=tuple(found))


def _hosts(root: _Table) -> tuple[Host, ...]:
    value = root.value("host")
    entries: list[object] = value if isinstance(value, list) else []
    if not entries:
        root.fail("host", "must be a non-empty array of tables")
    hosts: list[Host] = []
    for entry in entries:
        if not isinstance(entry, dict):
            root.fail("host", "must be a non-empty array of tables")
        values: dict[str, object] = entry
        table = _Table("[[agent.host]]", values)
        host = _host(table)
        if any(h.name == host.name for h in hosts):
            table.fail("name", f"{host.name!r} is defined twice")
        hosts.append(host)
    return tuple(hosts)


def _proven_harnesses(table: _Table) -> tuple[Harness, ...]:
    value = table.value("proven_harnesses")
    if not isinstance(value, list):
        table.fail("proven_harnesses", "must list distinct codex, claude or agy names")
    entries: list[object] = value
    if any(name not in ("codex", "claude", "agy") for name in entries) or len(
        entries
    ) != len(set(entries)):
        table.fail("proven_harnesses", "must list distinct codex, claude or agy names")
    return tuple(Harness(str(name)) for name in entries)


_PRESSURE_LEVELS = (1, 2, 4)


def _percent(table: _Table, key: str, default: int) -> int:
    if key not in table.values:
        return default
    value = table.integer(key)
    if value > _MAX_PERCENT:
        table.fail(key, "must be a positive integer of at most 100")
    return value


def _pressure_level(table: _Table) -> int:
    if "max_memory_pressure_level" not in table.values:
        return 4
    value = table.integer("max_memory_pressure_level")
    if value not in _PRESSURE_LEVELS:
        why = "must be 1 (normal), 2 (warn) or 4 (critical)"
        table.fail("max_memory_pressure_level", why)
    return value


def _host(table: _Table) -> Host:
    table.only(
        "name",
        "os",
        "shell",
        "account",
        "unsandboxed",
        "accounts",
        "proven_harnesses",
        "tbhbdev",
        "load_limit",
        "min_memory_available_percent",
        "max_swap_used_percent",
        "max_memory_pressure_avg10",
        "max_memory_pressure_level",
        "limits_warn_only",
        "build_shims",
        "max_concurrent_runs",
    )
    name = _check_name(table, "name", table.text("name"), HOST_NAME)
    os = table.choice("os", Os)
    shell = table.text("shell")
    if shell not in _PROGRAMS:
        table.fail("shell", f"must be one of {', '.join(_PROGRAMS)}")
    account = _check_name(table, "account", table.text("account"), NAME)
    unsandboxed = table.flag("unsandboxed") if "unsandboxed" in table.values else True
    accounts = table.strings("accounts")
    if not all(NAME.fullmatch(a) for a in accounts):
        table.fail("accounts", f"must list names matching {NAME.pattern}")
    if len(set(accounts)) != len(accounts):
        table.fail("accounts", "must not contain duplicates")
    if account not in accounts:
        table.fail("account", "must be one of accounts")
    program = table.path("tbhbdev") if "tbhbdev" in table.values else None
    if program is not None and not program.startswith("/"):
        table.fail("tbhbdev", "must be an absolute path")
    return Host(
        name,
        os,
        shell,
        account,
        unsandboxed,
        accounts,
        _proven_harnesses(table),
        program,
        load_limit=table.integer("load_limit")
        if "load_limit" in table.values
        else None,
        min_memory_available_percent=_percent(
            table, "min_memory_available_percent", 20
        ),
        max_swap_used_percent=_percent(table, "max_swap_used_percent", 80),
        max_memory_pressure_avg10=_percent(table, "max_memory_pressure_avg10", 10),
        max_memory_pressure_level=_pressure_level(table),
        limits_warn_only=table.flag("limits_warn_only")
        if "limits_warn_only" in table.values
        else False,
        build_shims=table.flag("build_shims")
        if "build_shims" in table.values
        else True,
        max_concurrent_runs=table.nonnegative(
            "max_concurrent_runs", DEFAULT_MAX_CONCURRENT_RUNS
        ),
    )


def _check_host_accounts(settings: Settings) -> None:
    known = {c.account for c in settings.credentials}
    for host in settings.hosts:
        for account in host.accounts:
            if account not in known:
                message = (
                    f"[[agent.host]] accounts names {account}, "
                    f"which has no [agent.credentials.{account}]"
                )
                raise Invalid(message)


def parse(agent: dict[str, object]) -> Settings:
    """The settings in AGENT, the user config's `[agent]` table.

    Raises:
        Invalid: A key is missing, unknown or invalid. The message names the
            table and the key.
    """
    root = _Table("[agent]", agent)
    root.only(
        "launch_wait_seconds",
        "agy_timeout",
        "agy_build_timeout",
        "kill_grace_seconds",
        "max_run_minutes",
        "run_memory_mib",
        "review_memory_mib",
        "sockets_dir",
        "public_repos",
        "otel",
        "sandbox",
        "codex",
        "credentials",
        "claude",
        "host",
        "ssh_connect_timeout",
        "ssh_keepalive_seconds",
        "ssh_keepalive_count_max",
        "host_tag",  # Another command reads this key.
        "worker",
    )
    sandbox = _sandbox(root.table("sandbox"))
    run_memory_mib = (
        root.integer("run_memory_mib")
        if "run_memory_mib" in agent
        else DEFAULT_RUN_MEMORY_MIB
    )
    settings = Settings(
        launch_wait_seconds=root.integer("launch_wait_seconds"),
        agy_timeout_seconds=root.duration("agy_timeout"),
        kill_grace_seconds=root.integer("kill_grace_seconds"),
        max_run_minutes=(
            root.integer("max_run_minutes") if "max_run_minutes" in agent else None
        ),
        sockets_dir=root.path("sockets_dir"),
        sandbox=sandbox,
        codex=_codex(root.table("codex") if "codex" in agent else None),
        credentials=_credentials(root.table("credentials")),
        claude=_claude(root.table("claude")),
        hosts=_hosts(root),
        agy_build_timeout_seconds=root.duration("agy_build_timeout")
        if "agy_build_timeout" in agent
        else 3600,
        ssh_connect_timeout=root.integer("ssh_connect_timeout")
        if "ssh_connect_timeout" in agent
        else 15,
        ssh_keepalive_seconds=root.integer("ssh_keepalive_seconds")
        if "ssh_keepalive_seconds" in agent
        else 30,
        ssh_keepalive_count_max=root.integer("ssh_keepalive_count_max")
        if "ssh_keepalive_count_max" in agent
        else 3,
        public_repos=_public_repos(root),
        otel_endpoint=_otel_endpoint(root, sandbox.network_domains),
        run_memory_mib=run_memory_mib,
        review_memory_mib=root.integer("review_memory_mib")
        if "review_memory_mib" in agent
        else run_memory_mib,
        worker_path=_worker_path(root),
    )
    _check_host_accounts(settings)
    return settings
