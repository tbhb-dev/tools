"""`tbhbdev host`: Connect tokens for agent hosts, and commands on them.

These commands run on the operator's Mac, which has 1Password Desktop and the
`op` CLI. The deployment's values come from the user config (see
userconfig.py): every `op` call names its `[op] account`, and agent hosts are
the tailnet nodes tagged with its `[agent] host_tag`.

Each host gets its own Connect token for its own vault, `[vaults] host_prefix`
and the host's name, the vault `[vaults] all`, and any others the operator
names, in the `agent` account's keychain. A token's vaults can't change, so a
change is a new token: create it, install it, check that it reaches its
vaults, then delete the old one. A token's name records its host, creation
time and vaults, so `op connect token list` alone tells what each token
reaches and when it expires.

A token travels only on stdin: from `op` to this process, then over ssh into
`security` or `secret-tool`. It never sits in argv, a file or a log line.
Bootstrap sends each host the agent's copy of the config the same way, and
never prints it. The copy is the operator's config with `[connect] endpoints`
set to `https://<host>.<suffix>` for each of `[connect] server_hosts`, where
the suffix is the tailnet's MagicDNS suffix from `tailscale status`.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shlex
import shutil
import socket
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev import runner, secrets, userconfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

type Json = dict[str, object]
# A finished command. A failed one is its CalledProcessError, which holds the
# same returncode, stdout and stderr.
type Done = subprocess.CompletedProcess[str] | subprocess.CalledProcessError

CREDENTIALS = "1password-credentials.json"
AGENT = "agent"
LIFETIME = timedelta(days=90)
LIFETIME_FLAG = "90d"
RENEW_WITHIN = timedelta(days=14)
STAMP = "%Y%m%dT%H%M%SZ"
TOKEN_NAME_PARTS = 4
SWIFT = "/usr/bin/swift"
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._-]+\Z")
# What each of `[connect] server_hosts` must be, so that its endpoint's host
# is that one label under the tailnet's suffix.
DNS_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
SSH = ("ssh", "-o", "BatchMode=yes")
TOUCH_ID_UNAVAILABLE = 2

# A Touch ID check through LocalAuthentication: exit 0 when it passes, 1 when
# it fails, 2 when this Mac can't do it.
TOUCH_ID = """\
import Foundation
import LocalAuthentication

let context = LAContext()
var error: NSError?
let policy = LAPolicy.deviceOwnerAuthenticationWithBiometrics
guard context.canEvaluatePolicy(policy, error: &error) else { exit(2) }
let done = DispatchSemaphore(value: 0)
var passed = false
context.evaluatePolicy(
    policy, localizedReason: "run a command as the operator on an agent host"
) { success, _ in
    passed = success
    done.signal()
}
done.wait()
exit(passed ? 0 : 1)
"""


class HostError(Exception):
    """A command for agent hosts failed. The message never holds a token."""


def now() -> datetime:
    return datetime.now(UTC)


def _run(
    argv: Sequence[str], *, stdin: str | None = None, cwd: Path | None = None
) -> Done:
    try:
        return subprocess.run(  # nosec B603
            # check comes first so that no other argument starts on its line.
            # Equivalent mutants: unchecked, a failed run returns what the handler does.
            check=True,  # pragma: no mutate
            args=argv,
            input=stdin,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except subprocess.CalledProcessError as failed:
        return failed
    except FileNotFoundError as exc:
        message = f"{argv[0]} not found"
        raise HostError(message) from exc


def _json(text: str, source: str) -> object:
    try:
        data: object = json.loads(text)
    except ValueError as exc:
        message = f"{source} printed something other than JSON"
        raise HostError(message) from exc
    return data


def _dicts(value: object) -> list[Json]:
    entries: list[object] = value if isinstance(value, list) else []
    return [e for e in entries if isinstance(e, dict)]


def _str(value: Json, key: str) -> str:
    entry = value.get(key)
    return entry if isinstance(entry, str) else ""


# Config.


def setting(table: str, key: str) -> str:
    """KEY in the user config's [TABLE], a non-empty string."""
    try:
        return userconfig.string(table, key)
    except userconfig.ConfigError as exc:
        raise HostError(str(exc)) from exc


def host_tag() -> str:
    """The Tailscale tag that marks an agent host."""
    return setting("agent", "host_tag")


def server_name() -> str:
    """The Connect server's name."""
    return setting("connect", "server")


def token_prefix() -> str:
    """The first part of the name of every token this tool creates."""
    return setting("connect", "token_prefix")


def server_hosts() -> list[str]:
    """The hosts that run the Connect server, each a DNS label.

    A message names a bad entry by its place in the list, never by its value.
    """
    try:
        names = userconfig.strings("connect", "server_hosts")
    except userconfig.ConfigError as exc:
        raise HostError(str(exc)) from exc
    for number, name in enumerate(names, start=1):
        if not DNS_LABEL.fullmatch(name):
            message = (
                f"{userconfig.path()}: entry {number} of [connect] server_hosts "
                "isn't a DNS label"
            )
            raise HostError(message)
    return names


# Hosts.


@dataclass(frozen=True)
class Host:
    name: str
    os: str
    local: bool


def _host(node: Json, tag: str, *, local: bool) -> Host | None:
    tags = node.get("Tags")
    if not isinstance(tags, list) or tag not in tags:
        return None
    name = (_str(node, "DNSName") or _str(node, "HostName")).split(".")[0]
    return Host(name.lower(), _str(node, "OS"), local)


def tailnet() -> Json:
    """`tailscale status --json`: the tailnet as this machine sees it."""
    tailscale = shutil.which("tailscale") or TAILSCALE_APP
    done = _run([tailscale, "status", "--json"])
    if done.returncode != 0:
        message = f"tailscale status failed: {done.stderr.strip()}"
        raise HostError(message)
    status = _dicts([_json(done.stdout, "tailscale status")])
    if not status:
        message = "tailscale status printed something other than an object"
        raise HostError(message)
    return status[0]


def hosts(tag: str) -> list[Host]:
    """The tailnet's hosts tagged TAG, this machine included when it is one."""
    status = tailnet()
    peers = status.get("Peer")
    nodes = [(status.get("Self"), True)]
    if isinstance(peers, dict):
        by_key: Json = peers
        nodes += [(peer, False) for peer in by_key.values()]
    found = [
        _host(node, tag, local=local) for node, local in nodes if isinstance(node, dict)
    ]
    return sorted((h for h in found if h is not None), key=lambda h: h.name)


def select(names: Sequence[str] | None, *, every: bool) -> list[Host]:
    """The hosts --host names, every host for --all, or else this machine."""
    tag = host_tag()
    tagged = hosts(tag)
    if every:
        return tagged
    if not names:
        local = [h for h in tagged if h.local]
        if not local:
            message = f"this machine isn't tagged {tag}; name --host or --all"
            raise HostError(message)
        return local
    by_name = {h.name: h for h in tagged}
    unknown = [n for n in names if n.lower() not in by_name]
    if unknown:
        message = f"not tagged {tag}: {', '.join(unknown)}"
        raise HostError(message)
    return [by_name[n.lower()] for n in dict.fromkeys(names)]


def endpoints() -> list[str]:
    """The Connect endpoints: each server host on the tailnet, in config order.

    The tailnet's suffix comes from tailscale alone, so neither a repository
    nor the operator's config holds it. A message never includes it.
    """
    suffix = _str(tailnet(), "MagicDNSSuffix")
    if not suffix:
        message = (
            "tailscale status gives no MagicDNSSuffix, "
            "so the Connect endpoints are unknown"
        )
        raise HostError(message)
    return [f"https://{h}.{suffix}" for h in server_hosts()]


# 1Password, through op.


def op(*args: str, cwd: Path | None = None) -> str:
    """Run op against the configured account and return its output."""
    done = _run(["op", *args, "--account", setting("op", "account")], cwd=cwd)
    if done.returncode != 0:
        message = f"op {shlex.join(args)} failed: {done.stderr.strip()}"
        raise HostError(message)
    return done.stdout


def op_list(*args: str) -> list[Json]:
    return _dicts(_json(op(*args, "--format", "json"), f"op {' '.join(args)}"))


def find_server() -> str | None:
    name = server_name()
    servers = op_list("connect", "server", "list")
    return next((_str(s, "id") for s in servers if _str(s, "name") == name), None)


def need_server() -> str:
    server = find_server()
    if server is None:
        message = f"there is no Connect server {server_name()}; run bootstrap first"
        raise HostError(message)
    return server


def ensure_server() -> str:
    """The Connect server's ID, creating the server when it doesn't exist.

    A new server's credentials file goes in a new 0700 directory. Only its
    path is printed.
    """
    server = find_server()
    if server is not None:
        return server
    where = server_hosts()
    name = server_name()
    directory = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    op("connect", "server", "create", name, cwd=directory)
    print(f"created the Connect server {name}")
    print(f"its credentials file is {directory / CREDENTIALS}")
    print(f"install it on {' and '.join(where)}, then delete it")
    return need_server()


def vault_names() -> set[str]:
    return {_str(v, "name") for v in op_list("vault", "list")}


def ensure_vaults(names: Iterable[str]) -> None:
    existing = vault_names()
    for name in sorted(set(names) - existing):
        op("vault", "create", name)
        print(f"created the vault {name}")


@dataclass(frozen=True)
class Token:
    """A Connect token, as its name describes it."""

    id: str
    name: str
    host: str
    created: datetime
    vaults: frozenset[str]

    @property
    def expires(self) -> datetime:
        return self.created + LIFETIME


def token_name(host: str, created: datetime, vaults: Iterable[str]) -> str:
    stamp = created.strftime(STAMP)
    return f"{token_prefix()}:{host}:{stamp}:{','.join(sorted(vaults))}"


def parse_token(entry: Json) -> Token | None:
    """The agent host token ENTRY describes, or None for any other token."""
    name = _str(entry, "name")
    parts = name.split(":", TOKEN_NAME_PARTS - 1)
    if len(parts) != TOKEN_NAME_PARTS or parts[0] != token_prefix():
        return None
    try:
        created = datetime.strptime(parts[2], STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return Token(
        _str(entry, "id"), name, parts[1], created, frozenset(parts[3].split(","))
    )


def live_tokens(server: str) -> tuple[list[Token], int]:
    """SERVER's live agent host tokens, and how many other live tokens it has."""
    entries = op_list("connect", "token", "list", "--server", server)
    live = [e for e in entries if _str(e, "state").upper() != "REVOKED"]
    parsed = [parse_token(e) for e in live]
    tokens = [t for t in parsed if t is not None]
    return tokens, len(parsed) - len(tokens)


@dataclass
class Account:
    """The Connect server and its live tokens, as of the last `reload`."""

    server: str
    when: datetime
    tokens: list[Token]
    foreign: int = 0

    @classmethod
    def load(cls, server: str) -> Account:
        tokens, foreign = live_tokens(server)
        return cls(server, now(), tokens, foreign)

    def reload(self) -> None:
        self.tokens, self.foreign = live_tokens(self.server)

    def current(self, host: Host) -> Token | None:
        """HOST's newest token."""
        mine = [t for t in self.tokens if t.host == host.name]
        return max(mine, key=lambda t: t.created, default=None)

    def in_use(self) -> set[str]:
        return {v for t in self.tokens for v in t.vaults}

    def grant(self, vaults: Iterable[str]) -> None:
        """Give the server the vaults no token on it reaches yet."""
        for vault in sorted(set(vaults) - self.in_use()):
            op("connect", "vault", "grant", "--server", self.server, "--vault", vault)
            print(f"granted the Connect server the vault {vault}")

    def delete(self, tokens: Iterable[Token]) -> None:
        for token in tokens:
            op("connect", "token", "delete", token.id, "--server", self.server)
            print(f"deleted the token {token.name}")

    def create(self, name: str, vaults: Iterable[str]) -> str:
        """A new token's value. It goes nowhere but the caller."""
        args = ["connect", "token", "create", name, "--server", self.server]
        for vault in sorted(vaults):
            args += ["--vault", vault]
        value = op(*args, "--expires-in", LIFETIME_FLAG).strip()
        if not TOKEN_SHAPE.match(value):
            message = f"op connect token create printed no token for {name}"
            raise HostError(message)
        return value


# The agent keychain on a host.

_KC = secrets.KEYCHAIN
_ITEM = f"-s {secrets.SERVICE} -a {secrets.ACCOUNT}"
_MAC_UNLOCK = f'security unlock-keychain -p "" {_KC}'
_MAC_FIND = f"security find-generic-password {_ITEM} {_KC} >/dev/null 2>&1"
_LOOKUP = f"secret-tool lookup service {secrets.SERVICE} account {secrets.ACCOUNT}"


def _linux(command: str) -> str:
    return shlex.join(secrets.linux_keyring(command))


# For each OS: set up the keychain, install the token from stdin, and report
# whether the keychain holds one. The install scripts end by checking the
# entry is there.
SCRIPTS: dict[str, dict[str, str]] = {
    "macOS": {
        "ensure": (
            'mkdir -p "$HOME/Library/Keychains" && '
            f'{{ [ -e "$HOME/Library/Keychains/{_KC}-db" ] || '
            f'security create-keychain -p "" {_KC}; }} && '
            f"{_MAC_UNLOCK} && security set-keychain-settings {_KC}"
        ),
        "install": (
            f"{_MAC_UNLOCK} && {{ security delete-generic-password {_ITEM} {_KC} "
            f">/dev/null 2>&1; security -i >/dev/null 2>&1; {_MAC_FIND}; }}"
        ),
        "present": (
            f"{_MAC_UNLOCK} >/dev/null 2>&1; "
            f"if {_MAC_FIND}; then echo present; else echo absent; fi"
        ),
    },
    "linux": {
        "ensure": _linux("true"),
        "install": _linux(
            f"secret-tool store --label='tbhbdev Connect token' "
            f"service {secrets.SERVICE} account {secrets.ACCOUNT} && "
            f"{_LOOKUP} >/dev/null"
        ),
        "present": _linux(
            f"if {_LOOKUP} >/dev/null; then echo present; else echo absent; fi"
        ),
    },
}


def _token_stdin(host: Host, value: str) -> str:
    if host.os == "linux":
        return value
    return f"add-generic-password -U {_ITEM} -T /usr/bin/security -w {value} {_KC}\n"


def on_host(host: Host, script: str, stdin: str | None = None) -> Done:
    """Run SCRIPT as the agent user on HOST, over ssh unless HOST is this one."""
    argv = ["sudo", "-n", "-u", AGENT, "-H", "sh", "-c", script]
    if not host.local:
        argv = [*SSH, host.name, shlex.join(argv)]
    return _run(argv, stdin=stdin)


def _script(host: Host, step: str) -> str:
    scripts = SCRIPTS.get(host.os)
    if scripts is None:
        message = f"{host.name}: unsupported OS {host.os!r}"
        raise HostError(message)
    return scripts[step]


def ensure_keychain(host: Host) -> None:
    done = on_host(host, _script(host, "ensure"))
    if done.returncode != 0:
        message = (
            f"{host.name}: cannot set up the agent keychain: {done.stderr.strip()}"
        )
        raise HostError(message)


def keychain_present(host: Host) -> bool:
    done = on_host(host, _script(host, "present"))
    answer = done.stdout.strip()
    if done.returncode != 0 or answer not in {"present", "absent"}:
        message = f"{host.name}: cannot check the agent keychain: {done.stderr.strip()}"
        raise HostError(message)
    return answer == "present"


def install(host: Host, value: str) -> None:
    script = _script(host, "install")
    done = on_host(host, script, _token_stdin(host, value))
    if done.returncode != 0:
        message = f"{host.name}: installing the token failed (exit {done.returncode})"
        raise HostError(message)


def verify(value: str, vaults: frozenset[str], urls: list[str]) -> None:
    """Check that the token VALUE lists exactly VAULTS through Connect at URLS."""
    try:
        listed = secrets.Connect(value, urls).vaults()
    except secrets.SecretsError as exc:
        message = f"the new token can't list its vaults: {exc}"
        raise HostError(message) from exc
    names = {str(v.get("name")) for v in listed}
    if names != vaults:
        message = f"the new token lists {sorted(names)}, not {sorted(vaults)}"
        raise HostError(message)


# The agent's copy of the user config on a host.

# Where the agent's tbhbdev reads its config. gitleaks flags the path, which
# holds no credential.
_CONFIG_DIR = 'd="${XDG_CONFIG_HOME:-$HOME/.config}/tbhbdev"'  # gitleaks:allow

# Each step reads the agent's copy from stdin and prints one of its
# answers. install writes a 0600 file in a 0700 directory through a rename,
# and only when the copy differs; check compares the copy without writing.
CONFIG_STEPS: dict[str, tuple[str, frozenset[str]]] = {
    "install": (
        (
            f'umask 077 && {_CONFIG_DIR} && mkdir -p "$d" && chmod 700 "$d" && '
            't=$(mktemp "$d/config.toml.XXXXXX") && trap \'rm -f "$t"\' EXIT && '
            'cat >"$t" && if cmp -s "$t" "$d/config.toml"; then '
            'chmod 600 "$d/config.toml" && echo ok; else '
            'chmod 600 "$t" && mv -f "$t" "$d/config.toml" && echo changed; fi'
        ),
        frozenset({"ok", "changed"}),
    ),
    "check": (
        (
            f'{_CONFIG_DIR}; if [ ! -e "$d/config.toml" ]; then echo missing; '
            'elif cmp -s - "$d/config.toml"; then echo matches; else echo differs; fi'
        ),
        frozenset({"matches", "differs", "missing"}),
    ),
}


def agent_copy(urls: list[str]) -> str:
    """The agent's copy of the config: the operator's, with URLS as endpoints.

    URLS replace any `[connect] endpoints` the operator's file has.
    """
    try:
        return userconfig.render("connect", "endpoints", urls)
    except userconfig.ConfigError as exc:
        raise HostError(str(exc)) from exc


def agent_config(host: Host, step: str, copy: str) -> str:
    """Install COPY as HOST's agent config, or check HOST's file against it."""
    script, answers = CONFIG_STEPS[step]
    done = on_host(host, script, copy)
    answer = done.stdout.strip()
    if done.returncode != 0 or answer not in answers:
        message = f"{host.name}: cannot {step} the agent's config.toml"
        message += f" (exit {done.returncode})"
        raise HostError(message)
    return answer


# Keeping a host's token current.


def host_vault(host: Host) -> str:
    return setting("vaults", "host_prefix") + host.name


def base_vaults(host: Host) -> frozenset[str]:
    return frozenset({host_vault(host), setting("vaults", "all")})


def problem(
    token: Token | None, vaults: frozenset[str], when: datetime, *, present: bool
) -> str | None:
    """Why HOST needs a new token, or None when its token is current."""
    if token is None:
        return "no token"
    if token.vaults != vaults:
        return f"the token {token.name} has other vaults"
    if token.expires - when <= RENEW_WITHIN:
        return f"the token {token.name} expires {token.expires:%Y-%m-%d}"
    if not present:
        return "the keychain has no token"
    return None


def refresh(
    account: Account, host: Host, vaults: frozenset[str], urls: list[str]
) -> None:
    """Give HOST a current token for exactly VAULTS, and delete its others.

    The new token's check reaches Connect at URLS.
    """
    mine = [t for t in account.tokens if t.host == host.name]
    current = account.current(host)
    why = problem(current, vaults, account.when, present=keychain_present(host))
    if current is not None and why is None:
        stale = [t for t in mine if t is not current]
        if stale:
            account.delete(stale)
            account.reload()
        print(f"{host.name}: the token {current.name} is current")
        return
    print(f"{host.name}: {why}; creating a new token")
    name = token_name(host.name, account.when, vaults)
    try:
        value = account.create(name, vaults)
        install(host, value)
        verify(value, vaults, urls)
    except HostError:
        account.reload()
        account.delete(t for t in account.tokens if t.name == name)
        raise
    account.delete(mine)
    account.reload()
    print(f"{host.name}: installed the token {name}")


def _plans(
    account: Account,
    chosen: list[Host],
    change: Callable[[frozenset[str]], frozenset[str]],
) -> dict[Host, frozenset[str]]:
    plans: dict[Host, frozenset[str]] = {}
    for host in chosen:
        current = account.current(host)
        held = current.vaults if current is not None else frozenset()
        plans[host] = change(base_vaults(host) | held)
    return plans


# The commands.


def _check_vaults(vaults: Sequence[str]) -> frozenset[str]:
    bad = [v for v in vaults if not v or "," in v]
    if bad:
        message = f"a vault name can't be empty or hold a comma: {bad!r}"
        raise HostError(message)
    return frozenset(vaults)


def cmd_list(_: argparse.Namespace) -> int:
    for host in hosts(host_tag()):
        print(f"{host.name}\t{host.os}{'\t(this machine)' if host.local else ''}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    named = _check_vaults(args.vaults)
    chosen = select(args.host, every=args.all)
    urls = endpoints()
    copy = agent_copy(urls)
    server = ensure_server()
    ensure_vaults({*named, *(v for h in chosen for v in base_vaults(h))})
    account = Account.load(server)
    plans = _plans(account, chosen, lambda held: held | named)
    account.grant(v for vaults in plans.values() for v in vaults)
    for host in chosen:
        ensure_keychain(host)
        print(f"{host.name}: config.toml {agent_config(host, 'install', copy)}")
        refresh(account, host, plans[host], urls)
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    named = _check_vaults(args.vaults)
    chosen = select(args.host, every=args.all)
    urls = endpoints()
    account = Account.load(need_server())
    plans = _plans(account, chosen, lambda held: held | named)
    needed = {v for vaults in plans.values() for v in vaults}
    missing = sorted(needed - vault_names())
    if missing:
        message = f"no such vault: {', '.join(missing)}; run bootstrap to create it"
        raise HostError(message)
    account.grant(needed)
    for host in chosen:
        refresh(account, host, plans[host], urls)
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    named = _check_vaults(args.vaults)
    chosen = select(args.host, every=args.all)
    for host in chosen:
        kept = sorted(named & base_vaults(host))
        if kept:
            message = f"{host.name} always keeps {', '.join(kept)}"
            raise HostError(message)
    urls = endpoints()
    account = Account.load(need_server())
    before = account.in_use()
    plans = _plans(account, chosen, lambda held: held - named)
    for host in chosen:
        refresh(account, host, plans[host], urls)
    unused = sorted((named & before) - account.in_use())
    if unused and account.foreign:
        print(
            f"leaving the server's access to {', '.join(unused)} alone: "
            f"{account.foreign} token(s) on it aren't agent host tokens"
        )
        return 0
    for vault in unused:
        op("connect", "vault", "revoke", "--server", account.server, "--vault", vault)
        print(f"revoked the Connect server's access to the vault {vault}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    chosen = select(args.host, every=args.all)
    copy = agent_copy(endpoints())
    server = find_server()
    account = Account.load(server) if server is not None else None
    for host in chosen:
        token = account.current(host) if account is not None else None
        entry = "present" if keychain_present(host) else "absent"
        tail = (
            f"keychain entry {entry}; config.toml {agent_config(host, 'check', copy)}"
        )
        if token is None:
            print(f"{host.name}: no token; {tail}")
            continue
        print(
            f"{host.name}: token {token.name}; "
            f"vaults {', '.join(sorted(token.vaults))}; "
            f"expires {token.expires:%Y-%m-%dT%H:%M:%SZ}; {tail}"
        )
    return 0


def touch_id() -> None:
    """Pass only when the person at this Mac passes a Touch ID check.

    This is the guard that keeps an agent from escalating through
    `exec --operator`. It has no bypass.
    """
    if platform.system() != "Darwin":
        message = "--operator needs macOS with Touch ID"
        raise HostError(message)
    done = _run([SWIFT, "-"], stdin=TOUCH_ID)
    if done.returncode == TOUCH_ID_UNAVAILABLE:
        message = "--operator needs Touch ID, which isn't available here"
        raise HostError(message)
    if done.returncode != 0:
        message = "the Touch ID check failed"
        raise HostError(message)


def cmd_exec(args: argparse.Namespace) -> int:
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        message = "exec needs a command after --"
        raise HostError(message)
    host: str | None = args.host
    if host is not None and not HOSTNAME.match(host):
        message = f"not a host name: {host!r}"
        raise HostError(message)
    if args.operator:
        touch_id()
    argv = command if args.operator else ["sudo", "-n", "-u", AGENT, "-H", *command]
    this = socket.gethostname().split(".")[0].lower()
    if host is not None and host.lower() != this:
        argv = [*SSH, host, shlex.join(argv)]
    return subprocess.call(argv)  # nosec B603


def _targets(parser: argparse.ArgumentParser) -> None:
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--host", action="append", help="an agent host (repeatable)")
    which.add_argument("--all", action="store_true", help="every agent host")


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Manage agent hosts.")
    sub = parser.add_subparsers(dest="command_name", required=True)

    sub.add_parser("list", help="list the agent hosts").set_defaults(run=cmd_list)

    for name, run, nargs, text in (
        ("bootstrap", cmd_bootstrap, "*", "set up hosts, idempotently"),
        ("grant", cmd_grant, "+", "add vaults to hosts' tokens"),
        ("revoke", cmd_revoke, "+", "take vaults from hosts' tokens"),
    ):
        command = sub.add_parser(name, help=text)
        _targets(command)
        command.add_argument("vaults", nargs=nargs, metavar="VAULT")
        command.set_defaults(run=run)

    status = sub.add_parser("status", help="show hosts' tokens and configs")
    _targets(status)
    status.set_defaults(run=cmd_status)

    run_on = sub.add_parser("exec", help="run a command on a host as the agent user")
    run_on.add_argument("--host", help="the host (default: this machine)")
    run_on.add_argument(
        "--operator", action="store_true", help="run as yourself, after Touch ID"
    )
    run_on.add_argument("command", nargs=argparse.REMAINDER)
    run_on.set_defaults(run=cmd_exec)
    return parser


def main(argv: list[str], prog: str) -> int:
    return runner.main(_parser(prog), argv, HostError)
