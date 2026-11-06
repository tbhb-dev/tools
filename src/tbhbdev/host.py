"""`tbhbdev host`: set up agent hosts, keep them current, and run commands on them.

These commands run on the operator's Mac, which has 1Password Desktop and the
`op` CLI. The deployment's values come from the user config (see
userconfig.py): every `op` call names its `[op] account`, and agent hosts are
the tailnet nodes tagged with its `[agent] host_tag`.

The work splits along one line. `bootstrap` does everything that needs root
or the operator's 1Password: the `devel` group and the `agent` user, the
workspace ACLs, the Connect server and vaults, and each host's token. Only
the operator runs it, after a Touch ID check, as `grant` and `revoke` do.
`upgrade` does what is safe to repeat unattended: the clones, the tools, a
check of the token, and the private gitleaks config. It needs no Touch ID, no
`op` and no sudo password, so agents run it, and bootstrap ends by running it.
The user config's optional `[workspace]` names the workspace root and its
clones.

The private gitleaks config comes from one 1Password field, `config` in the
item `gitleaks-private` in the vault `[vaults] all`, which `upgrade` reads
through Connect with the host's own token, as the token check reaches the
agent keychain. It checks the text before writing anything: the config must
extend `.gitleaks.toml` and define the `tbhb-private-canary` rule. It then
installs `private.toml` in `.config/gitleaks` under the agent's home and
under the operator's, each a 0600 file in a 0700 directory, written to a
temporary file there and renamed into place. The text goes to each account
only on stdin, and no message ever quotes it.

Bootstrap writes that field before its upgrade. It generates the config from
live sources: each owner's private repositories from gh, the tailnet from
`tailscale status`, the vaults, the Connect endpoints, the operator's user
names on the hosts it set up, and fixed patterns. Each rule's id is its
category and a number, so no id holds a value, and the config reaches op only
on stdin. A source that fails or finds nothing stops bootstrap before it
writes. It creates the item when it's missing and edits it only when the text
changed.

Each host gets its own Connect token for its own vault, `[vaults] host_prefix`
and the host's name, the vault `[vaults] all`, and any others the operator
names, in the `agent` account's keychain. A token's vaults can't change, so a
change is a new token: create it, install it, check that it reaches its
vaults, then delete the old one. A token's name records its host, creation
time and vaults, so `op connect token list` alone tells what each token
reaches and when it expires. The agent's home records the installed token's
name too, which is how `upgrade` knows its vaults and expiry without `op`.

A token travels only on stdin and stdout: from `op` to this process, then
over ssh into `security` or `secret-tool`, and back out for `upgrade`'s
check. It never sits in argv, a file or a log line. Bootstrap sends each host
the agent's copy of the config the same way, and never prints it. The copy is
the operator's config with `[connect] endpoints` set to `https://<host>.<suffix>`
for each of `[connect] server_hosts`, where the suffix is the tailnet's
MagicDNS suffix from `tailscale status`.
"""

from __future__ import annotations

import argparse
import functools
import json
import platform
import re
import shlex
import shutil
import socket
import subprocess  # nosec B404
import sys
import tempfile
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urlsplit

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

# The agent account, with the same ids on every host. Bootstrap creates them
# and never changes the ids, groups or home of an account that exists.
GROUP = "devel"
GID = 33835
AGENT_UID = 24368
AGENT_HOMES = {"macOS": "/Users/agent", "linux": "/home/agent"}
SHELLS = {"macOS": "/bin/zsh", "linux": "/bin/bash"}
# The exit status of getent for a key it doesn't find.
GETENT_MISSING = 2

REPO = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")
# The gh config of the agent's GitHub identity, which clones and fetches use,
# and the agent's tbhbdev config. Neither holds a credential; gitleaks flags
# any path into .config.
GH_AGENT = 'GH_CONFIG_DIR="$HOME/.config/gh-agent"'  # gitleaks:allow
AGENT_CONFIG = '"$HOME/.config/tbhbdev"'  # gitleaks:allow
# The file in the agent's home that names the token in its keychain.
TOKEN_NAME_FILE = f"{AGENT_CONFIG}/connect-token-name"
# The files that make a directory a mise project.
MISE_CONFIGS = (
    "mise.toml",
    ".mise.toml",
    "mise/config.toml",
    ".mise/config.toml",
    ".config/mise.toml",
    ".config/mise/config.toml",
    ".tool-versions",
)
# The private gitleaks config: the field it comes from, which the bootstrap
# generator writes, and what it must hold. CANARY matches the line
# tbhb_rules' gitleaks_tool.sh.tpl looks for.
GITLEAKS_ITEM = "gitleaks-private"
GITLEAKS_FIELD = "config"
GITLEAKS_EXTENDS = ".gitleaks.toml"
GITLEAKS_CANARY = "tbhb-private-canary"
CANARY = re.compile(
    rf"""^[ \t]*id[ \t]*=[ \t]*["']{GITLEAKS_CANARY}["'][ \t]*(?:#.*)?$""",
    re.MULTILINE,
)
GITLEAKS_FILE = ".config/gitleaks/private.toml"
# Run as the account that gets the file, with the config on stdin. It prints
# `ok` when the file is already the config, owned by the account at 0600 in a
# 0700 directory, and otherwise puts the config in place and prints `changed`.
GITLEAKS_INSTALL = f"""\
set -eu
umask 077
cd
file={GITLEAKS_FILE}
dir=${{file%/*}}
mkdir -p "$dir"
fixed=
if [ -z "$(find "$dir" -prune -perm 700)" ]; then chmod 700 "$dir"; fixed=1; fi
tmp=$(mktemp "$dir/.private.toml.XXXXXX")
trap 'rm -f "$tmp"' EXIT
cat >"$tmp"
if [ -z "$fixed" ] && [ -f "$file" ] && [ -O "$file" ] &&
  [ -n "$(find "$file" -prune -perm 600)" ] && cmp -s "$tmp" "$file"; then
  echo ok
else
  mv -f "$tmp" "$file"
  echo changed
fi
"""
# What bootstrap generates the private gitleaks config from. The private
# repositories of each `[gitleaks] owners` entry, and the first must have
# some. gh lists at most REPO_LIMIT, so a list that long may be cut short.
REPO_LIMIT = 1000
# A name shorter than NAME_MIN, or a common word, would match too much on its
# own: a repository's then matches only as owner/name, and other names get no
# rule. WORDS is the system dictionary, which lacks plurals and these terms.
NAME_MIN = 4
WORDS = Path("/usr/share/dict/words")
TECH_WORDS = frozenset({"config", "dotfiles", "github", "infra", "monorepo", "website"})
# Tailnet host names that share their first STEM_MIN characters give a stem,
# which matches the names to come as well.
STEM_MIN = 4
# The directories the tools write under .config, .cache and .local.
TOOL_DIRS = ("awrel", "gh-agent", "gitleaks", "tbhbdev")
# The rule categories, in the config's order, and each one's description. The
# rule ids are the category and a number, so no id holds a value.
GITLEAKS_RULES = {
    "private-repo": "A private repository's name",
    "tailnet-suffix": "The tailnet's MagicDNS suffix",
    "tailnet-name": "The tailnet's name",
    "tailnet-host": "A tailnet host name",
    "tailnet-stem": "The stem a family of tailnet host names shares",
    "tailnet-ipv4": "A tailnet IPv4 address",
    "tailnet-ipv6": "A tailnet IPv6 address",
    "home-path": "A path into the operator's or the agent's home",
    "checkout-path": "A path into the workspace checkouts",
    "tool-dir": "A path into a directory the tools write",
    "private-ipv4": "A private IPv4 address",
    "local-name": "A .local or .lan host name",
    "mac-address": "A MAC address",
    "op-reference": "A 1Password secret reference",
    "session-socket": "A session socket path",
    "account-name": "An operator's account name",
    "op-account": "The 1Password account",
    "op-vault": "A 1Password vault name",
    "op-item": "The name of a 1Password item the tools read",
    "endpoint": "A Connect endpoint's host name",
    "keychain": "A keychain, keyring or Connect server name",
}
CANARY_REGEX = rf"\b{GITLEAKS_CANARY}-[0-9a-f]{{8}}\b"
# An IPv4 octet, and an address bounded as the committed public-ipv4 rule
# bounds one.
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = r"(?:^|[^0-9A-Za-z.]){}(?:[^0-9.]|$)"
# What may come before a path's interesting part: anything but a space, a
# quote or the punctuation around a path.
_PATH = r"""[^\s"'`<>()\[\],;=]*"""
# A path that starts at $HOME or ~ names no one's home.
GENERIC_HOME = r"^(?:~|\$HOME|\$\{HOME\})/"
# What an upgrade step reports.
OK = "ok"
CHANGED = "changed"
FAILED = "failed"

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
    policy, localizedReason: "act as the operator on agent hosts"
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


def _why(done: Done) -> str:
    return done.stderr.strip() or f"exit {done.returncode}"


def _without(host: Host, exc: HostError) -> str:
    """EXC's message without a leading `HOST: `."""
    return str(exc).removeprefix(f"{host.name}: ")


def _report(host: Host, exc: HostError) -> None:
    print(f"{host.name}: {_without(host, exc)}", file=sys.stderr)


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


def gitleaks_ref() -> str:
    """The private gitleaks config's field, in the vault `[vaults] all`."""
    return f"op://{setting('vaults', 'all')}/{GITLEAKS_ITEM}/{GITLEAKS_FIELD}"


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


@dataclass(frozen=True)
class Workspace:
    """The workspace root, relative to the operator's home, and its clones."""

    root: PurePosixPath
    repos: tuple[str, ...]


def workspace() -> Workspace | None:
    """The user config's [workspace], or None when it has none."""
    try:
        settings = userconfig.table("workspace")
    except userconfig.ConfigError as exc:
        raise HostError(str(exc)) from exc
    if settings is None:
        return None
    root = settings.get("root")
    path = PurePosixPath(root) if isinstance(root, str) else PurePosixPath("/")
    if path.is_absolute() or not path.parts or ".." in path.parts:
        message = (
            f"{userconfig.path()}: [workspace] root must be a relative path below home"
        )
        raise HostError(message)
    repos = settings.get("repos", [])
    entries: list[object] = repos if isinstance(repos, list) else [None]
    names = [e for e in entries if isinstance(e, str) and REPO.match(e)]
    if len(names) != len(entries):
        message = f"{userconfig.path()}: [workspace] repos must list owner/name strings"
        raise HostError(message)
    return Workspace(path, tuple(names))


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
    # Only the import-time SCRIPTS table calls this, before mutmut can switch
    # in a mutant, so its mutants can never run.
    return shlex.join(secrets.linux_keyring(command))  # pragma: no mutate


# For each OS: set up the keychain; install the token from stdin; print
# `present` and the recorded token name, or `absent`; and print the recorded
# name and then the token. The install scripts end by checking the entry is
# there.
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
        "held": (
            f"{_MAC_UNLOCK} >/dev/null 2>&1; if {_MAC_FIND}; then echo present; "
            f"cat {TOKEN_NAME_FILE} 2>/dev/null || :; else echo absent; fi"
        ),
        "read": (
            f"{_MAC_UNLOCK} >/dev/null 2>&1; "
            f"{{ cat {TOKEN_NAME_FILE} 2>/dev/null || echo; }} && "
            f"security find-generic-password {_ITEM} -w {_KC}"
        ),
    },
    "linux": {
        "ensure": _linux("true"),
        "install": _linux(
            f"secret-tool store --label='tbhbdev Connect token' "
            f"service {secrets.SERVICE} account {secrets.ACCOUNT} && "
            f"{_LOOKUP} >/dev/null"
        ),
        "held": _linux(
            f"if {_LOOKUP} >/dev/null; then echo present; "
            f"cat {TOKEN_NAME_FILE} 2>/dev/null || :; else echo absent; fi"
        ),
        "read": _linux(
            f"{{ cat {TOKEN_NAME_FILE} 2>/dev/null || echo; }} && {_LOOKUP}"
        ),
    },
}


def _token_stdin(host: Host, value: str) -> str:
    if host.os == "linux":
        return value
    return f"add-generic-password -U {_ITEM} -T /usr/bin/security -w {value} {_KC}\n"


def _over(host: Host, argv: list[str], stdin: str | None = None) -> Done:
    """Run ARGV on HOST, over ssh unless HOST is this one."""
    if not host.local:
        argv = [*SSH, host.name, shlex.join(argv)]
    return _run(argv, stdin=stdin)


def on_host(host: Host, script: str, stdin: str | None = None) -> Done:
    """Run SCRIPT as the agent user on HOST."""
    return _over(host, ["sudo", "-n", "-u", AGENT, "-H", "sh", "-c", script], stdin)


def as_operator(host: Host, script: str, stdin: str | None = None) -> Done:
    """Run SCRIPT as the operator, the user ssh connects as, on HOST."""
    return _over(host, ["sh", "-c", script], stdin)


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


def keychain_held(host: Host) -> str | None:
    """The recorded name of the token in HOST's agent keychain, or None.

    An entry with no recorded name gives "".
    """
    done = on_host(host, _script(host, "held"))
    lines = [*done.stdout.splitlines(), ""]
    if done.returncode != 0 or lines[0] not in {"present", "absent"}:
        message = f"{host.name}: cannot check the agent keychain: {done.stderr.strip()}"
        raise HostError(message)
    return lines[1].strip() if lines[0] == "present" else None


def install(host: Host, value: str, name: str) -> None:
    """Put the token VALUE in HOST's agent keychain, and record its NAME."""
    record = (
        f"mkdir -p {AGENT_CONFIG} && "
        f"printf '%s\\n' {shlex.quote(name)} >{TOKEN_NAME_FILE}"
    )
    script = f"{_script(host, 'install')} && {record}"
    done = on_host(host, script, _token_stdin(host, value))
    if done.returncode != 0:
        message = f"{host.name}: installing the token failed (exit {done.returncode})"
        raise HostError(message)


def verify(
    value: str, vaults: frozenset[str], urls: list[str], who: str = "the new token"
) -> None:
    """Check that the token VALUE lists exactly VAULTS through Connect at URLS."""
    try:
        listed = secrets.Connect(value, urls).vaults()
    except secrets.SecretsError as exc:
        message = f"{who} can't list its vaults: {exc}"
        raise HostError(message) from exc
    names = {str(v.get("name")) for v in listed}
    if names != vaults:
        message = f"{who} lists {sorted(names)}, not {sorted(vaults)}"
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
    token: Token | None, vaults: frozenset[str], when: datetime, *, held: str | None
) -> str | None:
    """Why HOST needs a new token, or None when its token is current.

    HELD is the name the keychain records, as keychain_held gives it.
    """
    if token is None:
        return "no token"
    if token.vaults != vaults:
        return f"the token {token.name} has other vaults"
    if token.expires - when <= RENEW_WITHIN:
        return f"the token {token.name} expires {token.expires:%Y-%m-%d}"
    if held is None:
        return "the keychain has no token"
    if held != token.name:
        return f"the keychain holds {held or 'an unrecorded token'}, not {token.name}"
    return None


def refresh(
    account: Account, host: Host, vaults: frozenset[str], urls: list[str]
) -> None:
    """Give HOST a current token for exactly VAULTS, and delete its others.

    The new token's check reaches Connect at URLS.
    """
    mine = [t for t in account.tokens if t.host == host.name]
    current = account.current(host)
    why = problem(current, vaults, account.when, held=keychain_held(host))
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
        install(host, value, name)
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


# The agent account and the operator's membership of devel.


def identity(host: Host) -> tuple[str, PurePosixPath]:
    """The operator's user name and home on HOST."""
    done = as_operator(host, 'id -un && printf "%s\\n" "$HOME"')
    lines = done.stdout.splitlines()
    if done.returncode != 0 or len(lines) != 2 or not lines[1].startswith("/"):  # noqa: PLR2004
        message = f"{host.name}: cannot find out who you are there: {_why(done)}"
        raise HostError(message)
    return lines[0], PurePosixPath(lines[1])


def agent_home(host: Host) -> str:
    """Where a new agent user's home goes on HOST."""
    return AGENT_HOMES[host.os]


@dataclass(frozen=True)
class User:
    uid: int
    gid: int
    home: str


@dataclass(frozen=True)
class Group:
    gid: int
    members: frozenset[str]


@dataclass(frozen=True)
class Change:
    """A root-only change: what to report once it's made, and its commands."""

    what: str
    commands: tuple[str, ...]


def _number(host: Host, value: str, what: str) -> int:
    if not value.isdigit():
        message = f"{host.name}: {what} has no numeric id: {value!r}"
        raise HostError(message)
    return int(value)


_MAC_ACE = re.compile(rf"\s*\d+: group:{GROUP} (?:inherited )?allow (\S+)\Z")
# Every right a directory can grant but chown and writesecurity, and the
# rights a file can grant but those. Directories pass both kinds of entry on.
_MAC_DIR_RIGHTS = (
    "list,add_file,search,add_subdirectory,delete_child,delete,readattr,"
    "writeattr,readextattr,writeextattr,readsecurity"
)
_MAC_INHERIT = "file_inherit,directory_inherit"
_MAC_FILE_RIGHTS = (
    "read,write,append,execute,delete,readattr,writeattr,readextattr,"
    "writeextattr,readsecurity"
)


class Platform(ABC):
    """How one OS reads and makes accounts and ACLs. Reads run as the operator."""

    def __init__(self, host: Host) -> None:
        self.host = host

    def read(self, script: str) -> Done:
        return as_operator(self.host, script)

    def fail(self, what: str, done: Done) -> NoReturn:
        message = f"{self.host.name}: cannot read {what}: {_why(done)}"
        raise HostError(message)

    @abstractmethod
    def user(self, name: str) -> User | None: ...

    @abstractmethod
    def group(self, name: str) -> Group | None: ...

    @abstractmethod
    def uid_holder(self, uid: int) -> str | None: ...

    @abstractmethod
    def gid_holder(self, gid: int) -> str | None: ...

    @abstractmethod
    def create_group(self) -> tuple[str, ...]: ...

    @abstractmethod
    def create_user(self, home: str) -> tuple[str, ...]: ...

    @abstractmethod
    def add_member(self, user: str) -> tuple[str, ...]: ...

    @abstractmethod
    def can_traverse(self, path: PurePosixPath) -> bool: ...

    @abstractmethod
    def has_full(self, path: PurePosixPath) -> bool: ...

    @abstractmethod
    def traverse(self, path: PurePosixPath) -> tuple[str, ...]: ...

    @abstractmethod
    def full(self, root: PurePosixPath) -> tuple[str, ...]: ...


class MacOS(Platform):
    """Local directory records through dscl, and ACLs through chmod and ls."""

    def _record(self, path: str) -> dict[str, list[str]] | None:
        done = self.read(f"dscl . -read {shlex.quote(path)}")
        if done.returncode != 0:
            if "eDSRecordNotFound" in done.stderr:
                return None
            self.fail(path, done)
        record: dict[str, list[str]] = {}
        # Equivalent mutants: nothing reads the key of a stray leading value line.
        key = ""  # pragma: no mutate
        for line in done.stdout.splitlines():
            if line.startswith(" "):
                record.setdefault(key, []).append(line.strip())
                continue
            key, _, rest = line.partition(":")
            record[key] = rest.split()
        return record

    def _one(self, record: dict[str, list[str]], key: str, what: str) -> str:
        values = record.get(key, [])
        if len(values) != 1:
            message = f"{self.host.name}: {what} has no single {key}"
            raise HostError(message)
        return values[0]

    def user(self, name: str) -> User | None:
        record = self._record(f"/Users/{name}")
        if record is None:
            return None
        what = f"the user {name}"
        uid = _number(self.host, self._one(record, "UniqueID", what), what)
        gid = _number(self.host, self._one(record, "PrimaryGroupID", what), what)
        return User(uid, gid, self._one(record, "NFSHomeDirectory", what))

    def group(self, name: str) -> Group | None:
        record = self._record(f"/Groups/{name}")
        if record is None:
            return None
        what = f"the group {name}"
        gid = _number(self.host, self._one(record, "PrimaryGroupID", what), what)
        return Group(gid, frozenset(record.get("GroupMembership", [])))

    def _holder(self, kind: str, key: str, number: int) -> str | None:
        done = self.read(f"dscl . -list /{kind} {key}")
        if done.returncode != 0:
            self.fail(f"the {key}s of /{kind}", done)
        pairs = (line.split() for line in done.stdout.splitlines())
        return next((p[0] for p in pairs if p[1:] == [str(number)]), None)

    def uid_holder(self, uid: int) -> str | None:
        return self._holder("Users", "UniqueID", uid)

    def gid_holder(self, gid: int) -> str | None:
        return self._holder("Groups", "PrimaryGroupID", gid)

    @staticmethod
    def _create(path: str, *attributes: tuple[str, str]) -> tuple[str, ...]:
        # dscl -create on a record that exists overwrites its attributes, so
        # stop if one has appeared since bootstrap looked.
        guard = (
            f"if dscl . -read {path} >/dev/null 2>&1; then "
            f"echo '{path} appeared during bootstrap; leaving it alone' >&2; exit 1; fi"
        )
        sets = [f"dscl . -create {path} {k} {shlex.quote(v)}" for k, v in attributes]
        return (guard, f"dscl . -create {path}", *sets)

    def create_group(self) -> tuple[str, ...]:
        return self._create(
            f"/Groups/{GROUP}",
            ("PrimaryGroupID", str(GID)),
            ("RealName", GROUP),
            ("Password", "*"),
        )

    def create_user(self, home: str) -> tuple[str, ...]:
        return self._create(
            f"/Users/{AGENT}",
            ("UniqueID", str(AGENT_UID)),
            ("PrimaryGroupID", str(GID)),
            ("NFSHomeDirectory", home),
            ("UserShell", SHELLS["macOS"]),
            ("RealName", AGENT),
            ("Password", "*"),
            ("IsHidden", "1"),
        )

    def add_member(self, user: str) -> tuple[str, ...]:
        quoted = shlex.quote(user)
        return (f"dscl . -append /Groups/{GROUP} GroupMembership {quoted}",)

    def _allowed(self, path: PurePosixPath) -> set[str]:
        done = self.read(f"ls -led {shlex.quote(str(path))}")
        if done.returncode != 0:
            self.fail(f"the ACL of {path}", done)
        rights: set[str] = set()
        for line in done.stdout.splitlines():
            entry = _MAC_ACE.match(line)
            if entry:
                rights |= set(entry.group(1).split(","))
        return rights

    def can_traverse(self, path: PurePosixPath) -> bool:
        return "search" in self._allowed(path)

    def has_full(self, path: PurePosixPath) -> bool:
        wanted = f"{_MAC_DIR_RIGHTS},{_MAC_INHERIT}".split(",")
        return set(wanted) <= self._allowed(path)

    def traverse(self, path: PurePosixPath) -> tuple[str, ...]:
        entry = shlex.quote(f"group:{GROUP} allow search")
        return (f"chmod +a {entry} {shlex.quote(str(path))}",)

    def full(self, root: PurePosixPath) -> tuple[str, ...]:
        # chmod +a adds a second entry beside an equal one, so take each
        # entry off before putting it back. Taking off one that isn't there
        # fails, which is fine.
        where = shlex.quote(str(root))
        commands: list[str] = []
        for kind, rights in (
            ("-type d", f"{_MAC_DIR_RIGHTS},{_MAC_INHERIT}"),
            ("! -type d ! -type l", _MAC_FILE_RIGHTS),
        ):
            entry = shlex.quote(f"group:{GROUP} allow {rights}")
            find = f"find {where} {kind} -exec chmod"
            commands += [
                f"{find} -a {entry} {{}} + 2>/dev/null || :",
                f"{find} +a {entry} {{}} +",
            ]
        return tuple(commands)


class Linux(Platform):
    """Accounts through getent and the shadow tools, and POSIX ACLs."""

    def _entry(self, database: str, key: str) -> list[str] | None:
        done = self.read(f"getent {database} {shlex.quote(key)}")
        if done.returncode == GETENT_MISSING:
            return None
        if done.returncode != 0:
            self.fail(f"{database} {key}", done)
        return done.stdout.strip().split(":")

    def user(self, name: str) -> User | None:
        fields = self._entry("passwd", name)
        if fields is None:
            return None
        what = f"the user {name}"
        uid = _number(self.host, fields[2], what)
        return User(uid, _number(self.host, fields[3], what), fields[5])

    def group(self, name: str) -> Group | None:
        fields = self._entry("group", name)
        if fields is None:
            return None
        gid = _number(self.host, fields[2], f"the group {name}")
        return Group(gid, frozenset(m for m in fields[3].split(",") if m))

    def uid_holder(self, uid: int) -> str | None:
        fields = self._entry("passwd", str(uid))
        return None if fields is None else fields[0]

    def gid_holder(self, gid: int) -> str | None:
        fields = self._entry("group", str(gid))
        return None if fields is None else fields[0]

    def create_group(self) -> tuple[str, ...]:
        return (f"groupadd --gid {GID} {GROUP}",)

    def create_user(self, home: str) -> tuple[str, ...]:
        useradd = (
            f"useradd --uid {AGENT_UID} --gid {GID} --create-home "
            f"--home-dir {shlex.quote(home)} --shell {SHELLS['linux']} {AGENT}"
        )
        return (useradd, f"usermod --lock {AGENT}")

    def add_member(self, user: str) -> tuple[str, ...]:
        return (f"usermod --append --groups {GROUP} {shlex.quote(user)}",)

    def _acl(self, path: PurePosixPath) -> dict[str, str]:
        done = self.read(
            f"getfacl --omit-header --absolute-names {shlex.quote(str(path))}"
        )
        if done.returncode != 0:
            self.fail(f"the ACL of {path}", done)
        entries: dict[str, str] = {}
        for line in done.stdout.splitlines():
            tag, _, rights = line.split("#")[0].strip().rpartition(":")
            entries[tag] = rights
        return entries

    def can_traverse(self, path: PurePosixPath) -> bool:
        # Equivalent mutants: any default without a lowercase x reads as no access.
        return "x" in self._acl(path).get(
            f"group:{GROUP}",
            "",  # pragma: no mutate
        )

    def has_full(self, path: PurePosixPath) -> bool:
        acl = self._acl(path)
        tags = (f"group:{GROUP}", f"default:group:{GROUP}")
        # Equivalent mutants: any default without r, w and x reads as no access.
        return all(
            set("rwx")
            <= set(
                acl.get(
                    tag,
                    "",  # pragma: no mutate
                )
            )
            for tag in tags
        )

    def traverse(self, path: PurePosixPath) -> tuple[str, ...]:
        return (f"setfacl -m group:{GROUP}:--x {shlex.quote(str(path))}",)

    def full(self, root: PurePosixPath) -> tuple[str, ...]:
        where = shlex.quote(str(root))
        return (
            f"setfacl -R -P -m group:{GROUP}:rwX {where}",
            f"find {where} -type d -exec setfacl -m default:group:{GROUP}:rwx {{}} +",
        )


PLATFORMS: dict[str, type[Platform]] = {"macOS": MacOS, "linux": Linux}


def platform_for(host: Host) -> Platform:
    kind = PLATFORMS.get(host.os)
    if kind is None:
        message = f"{host.name}: unsupported OS {host.os!r}"
        raise HostError(message)
    return kind(host)


def _plan_group(host: Host, system: Platform) -> tuple[Group | None, list[Change]]:
    group = system.group(GROUP)
    if group is None:
        holder = system.gid_holder(GID)
        if holder is not None:
            message = f"{host.name}: gid {GID} belongs to {holder}, not {GROUP}"
            raise HostError(message)
        what = f"created the group {GROUP} with gid {GID}"
        return None, [Change(what, system.create_group())]
    if group.gid != GID:
        message = (
            f"{host.name}: the group {GROUP} has gid {group.gid}; it should have {GID}"
        )
        raise HostError(message)
    print(f"{host.name}: the group {GROUP} already has gid {GID}")
    return group, []


def _plan_user(host: Host, system: Platform) -> tuple[str, list[Change]]:
    user = system.user(AGENT)
    if user is None:
        holder = system.uid_holder(AGENT_UID)
        if holder is not None:
            message = f"{host.name}: uid {AGENT_UID} belongs to {holder}, not {AGENT}"
            raise HostError(message)
        home = agent_home(host)
        what = f"created the user {AGENT} with uid {AGENT_UID} and gid {GID}"
        return home, [Change(what, system.create_user(home))]
    if (user.uid, user.gid) != (AGENT_UID, GID):
        message = (
            f"{host.name}: the user {AGENT} has uid {user.uid} and gid {user.gid}; "
            f"it should have uid {AGENT_UID} and gid {GID}"
        )
        raise HostError(message)
    print(f"{host.name}: the user {AGENT} already has uid {AGENT_UID} and gid {GID}")
    return user.home, []


def plan_accounts(host: Host, system: Platform, operator: str) -> list[Change]:
    """The changes that give HOST the devel group and the agent user.

    Every check runs before any change, so a host with a wrong id is left
    as it was.
    """
    group, changes = _plan_group(host, system)
    home, made = _plan_user(host, system)
    changes += made
    if group is not None and operator in group.members:
        print(f"{host.name}: {operator} is already in the group {GROUP}")
    else:
        what = f"added {operator} to the group {GROUP}"
        changes.append(Change(what, system.add_member(operator)))
    where = shlex.quote(home)
    if as_operator(host, f"[ -e {where} ]").returncode == 0:
        print(f"{host.name}: the agent's home {home} already exists")
    else:
        command = f"install -d -o {AGENT_UID} -g {GID} -m 0700 {where}"
        changes.append(Change(f"created the agent's home {home}", (command,)))
    return changes


# Workspace access for devel: traverse on the way down, everything below.


def _above(home: PurePosixPath, ws: Workspace) -> list[PurePosixPath]:
    """The directories from HOME down to the workspace root's parent."""
    parts = ws.root.parts
    return [home.joinpath(*parts[:i]) for i in range(len(parts))]


def access_gaps(
    system: Platform, home: PurePosixPath, ws: Workspace
) -> tuple[list[PurePosixPath], bool]:
    """The directories devel can't traverse, and whether the root lacks access."""
    blocked = [d for d in _above(home, ws) if not system.can_traverse(d)]
    return blocked, not system.has_full(home / ws.root)


def plan_access(
    host: Host, system: Platform, home: PurePosixPath, ws: Workspace, *, repair: bool
) -> list[Change]:
    """The changes that give devel the workspace, creating its root first."""
    root = home / ws.root
    where = shlex.quote(str(root))
    done = as_operator(host, f"[ -d {where} ] || {{ mkdir -p {where} && echo made; }}")
    if done.returncode != 0:
        message = f"{host.name}: cannot create {root}: {_why(done)}"
        raise HostError(message)
    if done.stdout.strip():
        print(f"{host.name}: created {root}")
    blocked, lacking = access_gaps(system, home, ws)
    changes = [Change(f"let {GROUP} traverse {d}", system.traverse(d)) for d in blocked]
    if lacking or repair:
        what = f"gave {GROUP} read, write and create on {root} and below"
        changes.append(Change(what, system.full(root)))
    if not changes:
        print(f"{host.name}: {GROUP} already has access to {root}")
    return changes


def privileged(host: Host, changes: list[Change]) -> None:
    """Make CHANGES as root on HOST, in one script.

    With passwordless sudo the script runs unattended. Otherwise sudo asks
    for the operator's password once, on this terminal or over `ssh -t`.
    The password goes only to sudo.
    """
    if not changes:
        return
    script = "\n".join(["set -eu", *(c for change in changes for c in change.commands)])
    if _over(host, ["sudo", "-n", "true"]).returncode == 0:
        done = _over(host, ["sudo", "-n", "sh", "-c", script])
        if done.returncode != 0:
            message = f"{host.name}: the setup as root failed: {_why(done)}"
            raise HostError(message)
    else:
        print(f"{host.name}: sudo needs your password", file=sys.stderr)
        argv = ["sudo", "sh", "-c", script]
        if not host.local:
            argv = [*SSH, "-t", host.name, shlex.join(argv)]
        code = subprocess.call(argv)  # nosec B603
        if code != 0:
            message = f"{host.name}: the setup as root failed (exit {code})"
            raise HostError(message)
    for change in changes:
        print(f"{host.name}: {change.what}")


PROBE = (
    "cd {root} && ls -A >/dev/null && probe=$(mktemp .tbhbdev-probe.XXXXXX) "
    '|| exit 1; printf probe >"$probe" && [ "$(cat "$probe")" = probe ]; '
    'status=$?; rm -f "$probe"; exit "$status"'
)


def probe(host: Host, root: PurePosixPath) -> None:
    """Check, as the agent, that it can list, write and read ROOT."""
    done = on_host(host, PROBE.format(root=shlex.quote(str(root))))
    if done.returncode != 0:
        message = f"{host.name}: the agent can't read and write {root}: {_why(done)}"
        raise HostError(message)
    print(f"{host.name}: the agent can read and write {root}")


def set_up(host: Host, ws: Workspace | None, *, repair: bool) -> str:
    """HOST's accounts and workspace access, ending with the agent's probe.

    Returns the operator's user name there.
    """
    system = platform_for(host)
    operator, home = identity(host)
    changes = plan_accounts(host, system, operator)
    if ws is not None:
        changes += plan_access(host, system, home, ws, repair=repair)
    privileged(host, changes)
    if ws is not None:
        probe(host, home / ws.root)
    return operator


# Upgrade: steps that are safe to repeat, each reporting ok, changed or failed.


@dataclass(frozen=True)
class Result:
    status: str
    message: str


@dataclass(frozen=True)
class Target:
    """The host a step works on, its workspace root there, and the Connect URLs."""

    host: Host
    root: PurePosixPath
    urls: list[str]


type Step = Callable[[Target], Result]


def _bootstrap_hint(host: Host) -> str:
    return f"run `tbhbdev host bootstrap --host {host.name}`"


def _git(target: Target, path: str, *args: str) -> Done:
    return as_operator(target.host, f"git -C {path} {shlex.join(args)}")


def _left_alone(target: Target, path: str, default: str) -> Result | None:
    """Why the clone at PATH isn't fast-forwarded, or None when it can be."""
    branch = _git(target, path, "symbolic-ref", "--quiet", "--short", "HEAD")
    name = branch.stdout.strip()
    if branch.returncode != 0 or name != default:
        return Result(OK, f"left alone: on {name or 'a detached HEAD'}, not {default}")
    status = _git(target, path, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return Result(FAILED, f"git status failed: {_why(status)}")
    if status.stdout.strip():
        return Result(OK, "left alone: tracked files are modified or staged")
    return None


def _fast_forward(target: Target, path: str, default: str) -> Result:
    upstream = f"origin/{default}"
    heads = _git(target, path, "rev-parse", "HEAD", upstream).stdout.split()
    if len(heads) != 2:  # noqa: PLR2004
        return Result(FAILED, f"cannot resolve HEAD and {upstream}")
    head, tip = heads
    if head == tip:
        return Result(OK, f"up to date at {head[:12]}")
    ahead = _git(target, path, "merge-base", "--is-ancestor", "HEAD", upstream)
    if ahead.returncode != 0:
        return Result(OK, f"left alone: {default} has commits {upstream} lacks")
    merged = _git(target, path, "merge", "--ff-only", "--quiet", upstream)
    if merged.returncode != 0:
        return Result(FAILED, f"the fast-forward failed: {_why(merged)}")
    return Result(CHANGED, f"fast-forwarded {default} from {head[:12]} to {tip[:12]}")


def clone(repo: str, target: Target) -> Result:
    """Clone REPO as the agent's GitHub identity, or fast-forward a clean clone."""
    full = target.root / repo
    path = shlex.quote(str(full))
    if as_operator(target.host, f"[ -e {path} ]").returncode != 0:
        script = (
            f"mkdir -p {shlex.quote(str(full.parent))} && "
            f"{GH_AGENT} gh repo clone {shlex.quote(repo)} {path} -- --quiet"
        )
        done = as_operator(target.host, script)
        if done.returncode != 0:
            return Result(FAILED, f"the clone failed: {_why(done)}")
        return Result(CHANGED, f"cloned into {full}")
    head = _git(target, path, "rev-parse", "--abbrev-ref", "origin/HEAD")
    default = head.stdout.strip().removeprefix("origin/")
    if head.returncode != 0 or not default:
        return Result(FAILED, f"cannot find the default branch: {_why(head)}")
    held = _left_alone(target, path, default)
    if held is not None:
        return held
    helper = shlex.quote("credential.helper=!gh auth git-credential")
    fetch = as_operator(
        target.host,
        f"{GH_AGENT} git -C {path} -c credential.helper= -c {helper} "
        "fetch --quiet origin",
    )
    if fetch.returncode != 0:
        return Result(FAILED, f"the fetch failed: {_why(fetch)}")
    return _fast_forward(target, path, default)


def tools(target: Target) -> Result:
    """Install the tools the workspace root's mise config names."""
    root = f"cd {shlex.quote(str(target.root))}"
    found = " || ".join(f"[ -e {shlex.quote(c)} ]" for c in MISE_CONFIGS)
    if as_operator(target.host, f"{root} && {{ {found}; }}").returncode != 0:
        return Result(OK, f"no mise config in {target.root}")
    if (
        as_operator(target.host, f"{root} && mise install --dry-run-code").returncode
        == 0
    ):
        return Result(OK, "every tool is installed")
    done = as_operator(target.host, f"{root} && mise install")
    if done.returncode != 0:
        return Result(FAILED, f"mise install failed: {_why(done)}")
    return Result(CHANGED, "installed the missing tools")


def _keychain_token(host: Host) -> tuple[str, str]:
    """The recorded name and the token in HOST's agent keychain.

    The token is "" when the keychain has none.
    """
    done = on_host(host, _script(host, "read"))
    recorded, _, value = done.stdout.partition("\n")
    return recorded.strip(), value.strip() if done.returncode == 0 else ""


def token_health(target: Target) -> Result:
    """Whether the agent keychain's token reaches its vaults. Changes nothing."""
    host = target.host
    recorded, value = _keychain_token(host)
    hint = _bootstrap_hint(host)
    if not value:
        return Result(FAILED, f"the agent keychain has no token; {hint}")
    token = parse_token({"name": recorded})
    if token is None:
        return Result(FAILED, f"the keychain's token has no recorded name; {hint}")
    expires = f"{token.expires:%Y-%m-%d}"
    if token.expires - now() <= RENEW_WITHIN:
        return Result(FAILED, f"the token {token.name} expires {expires}; {hint}")
    try:
        verify(value, token.vaults, target.urls, f"the token {token.name}")
    except HostError as exc:
        return Result(FAILED, f"{exc}; {hint}")
    return Result(
        OK, f"the token {token.name} reaches its vaults; it expires {expires}"
    )


def gitleaks_problem(text: str) -> str | None:
    """Why TEXT isn't the private gitleaks config, or None. It never quotes TEXT."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return f"{gitleaks_ref()} isn't valid TOML"
    extend = data.get("extend")
    if not isinstance(extend, dict) or extend.get("path") != GITLEAKS_EXTENDS:
        return f'{gitleaks_ref()} has no [extend] path = "{GITLEAKS_EXTENDS}"'
    if not CANARY.search(text):
        return f"{gitleaks_ref()} has no rule with the id {GITLEAKS_CANARY}"
    return None


def private_gitleaks(host: Host, urls: list[str]) -> str:
    """The private gitleaks config, read with HOST's own token and checked."""
    _, value = _keychain_token(host)
    if not value:
        message = f"the agent keychain has no token; {_bootstrap_hint(host)}"
        raise HostError(message)
    try:
        store = secrets.Store(secrets.Connect(value, urls))
        text = store.read(gitleaks_ref())
    except secrets.SecretsError as exc:
        message = f"cannot read {gitleaks_ref()}: {exc}"
        raise HostError(message) from exc
    problem = gitleaks_problem(text)
    if problem is not None:
        raise HostError(problem)
    return text


def gitleaks_config(run: Callable[[Host, str, str], Done], target: Target) -> Result:
    """Install the private gitleaks config for the account RUN runs as.

    Nothing is written unless the config passes its check.
    """
    try:
        text = private_gitleaks(target.host, target.urls)
    except HostError as exc:
        return Result(FAILED, f"{exc}; wrote nothing")
    done = run(target.host, GITLEAKS_INSTALL, text)
    if done.returncode != 0:
        return Result(FAILED, f"cannot install {GITLEAKS_FILE}: {_why(done)}")
    if done.stdout.strip() == OK:
        return Result(OK, f"{GITLEAKS_FILE} is current")
    return Result(CHANGED, f"wrote {GITLEAKS_FILE}")


def upgrade_steps(ws: Workspace | None) -> list[tuple[str, Step]]:
    """The steps upgrade runs on each host, in order. New steps go here."""
    steps: list[tuple[str, Step]] = []
    if ws is not None:
        steps.extend((f"clone {r}", functools.partial(clone, r)) for r in ws.repos)
        steps.append(("tools", tools))
    steps.append(("token", token_health))
    steps += [
        ("gitleaks-config agent", functools.partial(gitleaks_config, on_host)),
        ("gitleaks-config operator", functools.partial(gitleaks_config, as_operator)),
    ]
    return steps


def upgrade(chosen: list[Host], ws: Workspace | None, urls: list[str]) -> list[str]:
    """Run every upgrade step on each host in CHOSEN; the hosts where one failed."""
    steps = upgrade_steps(ws)
    failed: list[str] = []
    for host in chosen:
        try:
            platform_for(host)
            _, home = identity(host)
        except HostError as exc:
            _report(host, exc)
            failed.append(host.name)
            continue
        target = Target(host, home / ws.root if ws is not None else home, urls)
        results: list[Result] = []
        for name, step in steps:
            try:
                result = step(target)
            except HostError as exc:
                result = Result(FAILED, _without(host, exc))
            print(f"{host.name}: {name}: {result.status}: {result.message}")
            results.append(result)
        if any(r.status == FAILED for r in results):
            failed.append(host.name)
    return failed


def access_state(host: Host, ws: Workspace | None) -> str:
    """HOST's workspace access, read without changing anything."""
    if ws is None:
        return "not configured"
    system = platform_for(host)
    _, home = identity(host)
    root = home / ws.root
    if as_operator(host, f"[ -d {shlex.quote(str(root))} ]").returncode != 0:
        return f"{root} doesn't exist"
    blocked, lacking = access_gaps(system, home, ws)
    missing = [*blocked, *([root] if lacking else [])]
    if missing:
        return f"{GROUP} is missing entries on {', '.join(map(str, missing))}"
    return f"complete for {GROUP} on {root}"


# Generating the private gitleaks config. Each source is a function of its
# own, which another can stand in for. A source that fails or finds nothing
# stops the generator before it writes anything, and no message quotes what a
# source found. The config reaches op only on stdin.


@dataclass(frozen=True)
class Tailnet:
    """The tailnet's MagicDNS suffix and name, and its nodes' DNS names."""

    suffix: str
    name: str
    hosts: tuple[str, ...]


@dataclass(frozen=True)
class Facts:
    """What the private gitleaks config keeps out of a repository."""

    repos: tuple[tuple[str, str], ...]
    tailnet: Tailnet
    words: frozenset[str]
    operators: tuple[str, ...]
    checkout: PurePosixPath | None
    vaults: tuple[str, ...]
    items: tuple[str, ...]
    endpoints: tuple[str, ...]
    names: tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    category: str
    regex: str
    allow: tuple[str, ...] = ()


def private_repos() -> list[tuple[str, str]]:
    """Each `[gitleaks] owners` entry's private repositories, as (owner, name)."""
    owners = userconfig.strings("gitleaks", "owners")
    found: list[tuple[str, str]] = []
    for owner in owners:
        source = f"gh repo list {owner}"
        done = _run(
            [
                "gh",
                "repo",
                "list",
                owner,
                "--visibility",
                "private",
                "--limit",
                str(REPO_LIMIT),
                "--json",
                "name",
            ]
        )
        if done.returncode != 0:
            message = f"{source} failed: {_why(done)}"
            raise HostError(message)
        data = _json(done.stdout, source)
        names = [_str(r, "name") for r in _dicts(data)]
        if not isinstance(data, list) or len(names) != len(data) or not all(names):
            message = f"{source} printed something other than named repositories"
            raise HostError(message)
        if len(names) >= REPO_LIMIT:
            message = f"{source} reached its limit of {REPO_LIMIT} and may miss some"
            raise HostError(message)
        if not names and owner == owners[0]:
            message = f"{source} listed no private repositories"
            raise HostError(message)
        found += [(owner, name) for name in names]
    return found


def _tailscale(binary: str) -> str:
    found = shutil.which(binary)
    if found is not None:
        return found
    if Path(TAILSCALE_APP).is_file():
        return TAILSCALE_APP
    message = f"{binary} isn't on PATH and {TAILSCALE_APP} doesn't exist"
    raise HostError(message)


def tailnet_facts(binary: str = "tailscale") -> Tailnet:
    """The tailnet this machine is on, from BINARY's `status --json`."""
    source = "tailscale status"
    done = _run([_tailscale(binary), "status", "--json"])
    if done.returncode != 0:
        message = f"{source} failed: {_why(done)}"
        raise HostError(message)
    status = _dicts([_json(done.stdout, source)])
    if not status:
        message = f"{source} printed something other than an object"
        raise HostError(message)
    current = _dicts([status[0].get("CurrentTailnet")])
    peers = status[0].get("Peer")
    nodes = [status[0].get("Self")]
    if isinstance(peers, dict):
        others: dict[str, object] = peers
        nodes += others.values()
    names = {_str(n, "DNSName").split(".")[0].lower() for n in _dicts(nodes)}
    found = Tailnet(
        _str(status[0], "MagicDNSSuffix"),
        _str(current[0], "Name") if current else "",
        tuple(sorted(names - {""})),
    )
    for what, value in (
        ("MagicDNSSuffix", found.suffix),
        ("CurrentTailnet.Name", found.name),
        ("DNSName", found.hosts),
    ):
        if not value:
            message = f"{source} has no {what}"
            raise HostError(message)
    return found


def common_words() -> frozenset[str]:
    """The words too common for a name to match alone, lowercased."""
    try:
        words = frozenset(WORDS.read_bytes().decode().lower().split())
    except (OSError, UnicodeError) as exc:
        message = f"cannot read {WORDS}"
        raise HostError(message) from exc
    if not words:
        message = f"{WORDS} lists no words"
        raise HostError(message)
    return words | TECH_WORDS


def endpoint_hosts(urls: list[str]) -> list[str]:
    """The host names in the Connect endpoints' URLS, which endpoints() made."""
    return sorted({str(urlsplit(url).hostname) for url in urls})


def gitleaks_facts(
    operators: Iterable[str], ws: Workspace | None, urls: list[str]
) -> Facts:
    """What the config keeps out, from each source in turn.

    OPERATORS are the operator's user names on the hosts bootstrap set up, and
    URLS the Connect endpoints.
    """
    vaults = vault_names() - {""}
    if not vaults:
        message = "op vault list listed no vaults"
        raise HostError(message)
    return Facts(
        repos=tuple(private_repos()),
        tailnet=tailnet_facts(),
        words=common_words(),
        operators=tuple(sorted(set(operators))),
        checkout=ws.root if ws is not None else None,
        vaults=tuple(sorted(vaults)),
        items=(GITLEAKS_ITEM,),
        endpoints=tuple(endpoint_hosts(urls)),
        names=tuple(sorted({secrets.KEYCHAIN, secrets.SERVICE, server_name()})),
    )


def _common(name: str, words: frozenset[str]) -> bool:
    """Whether NAME is too short, or too common a word, to match alone."""
    word = name.lower().removeprefix(".")
    forms = {word, word.removesuffix("s"), word.removesuffix("es")}
    return len(word) < NAME_MIN or not forms.isdisjoint(words)


def _bound(text: str, at: int) -> str:
    """A word boundary, when TEXT[AT] is an ASCII word character as RE2 sees it."""
    return r"\b" if re.fullmatch(r"[A-Za-z0-9_]", text[at]) else ""


def _literal(value: str) -> str:
    """A case-blind regex for VALUE as a whole word."""
    return f"(?i){_bound(value, 0)}{re.escape(value)}{_bound(value, -1)}"


def _named(
    category: str, values: Iterable[str], words: frozenset[str]
) -> tuple[list[Rule], int]:
    """A rule for each of VALUES that isn't common, and how many were."""
    every = sorted(set(values))
    kept = [v for v in every if not _common(v, words)]
    return [Rule(category, _literal(v)) for v in kept], len(every) - len(kept)


def _repos(facts: Facts) -> tuple[list[Rule], int]:
    """A rule for each repository, which a common name matches only scoped."""
    rules: list[Rule] = []
    scoped_only = 0
    for owner, name in sorted(set(facts.repos)):
        scoped = rf"\b{re.escape(owner)}/"
        if _common(name, facts.words):
            scoped_only += 1
        else:
            scoped = f"(?:{scoped}|{_bound(name, 0)})"
        rules.append(
            Rule("private-repo", f"(?i){scoped}{re.escape(name)}{_bound(name, -1)}")
        )
    return rules, scoped_only


def _shared(names: Sequence[str]) -> str:
    """The longest prefix all of NAMES have."""
    first, last = min(names), max(names)
    # LAST sorts after FIRST, so it differs from FIRST before it runs out.
    for at, mine in enumerate(first):
        if mine != last[at]:
            return first[:at]
    return first


def stems(hosts: Iterable[str]) -> list[str]:
    """The prefixes each family of host names sharing STEM_MIN characters has."""
    families: dict[str, list[str]] = {}
    for host in hosts:
        families.setdefault(host[:STEM_MIN], []).append(host)
    found = {
        re.sub(r"[-._0-9]+\Z", "", _shared(family))
        for family in families.values()
        if len(family) > 1
    }
    return sorted(found)


def _ipv4(category: str, *addresses: str) -> list[Rule]:
    return [Rule(category, _IPV4.format(a)) for a in addresses]


def gitleaks_rules(facts: Facts) -> tuple[list[Rule], dict[str, int]]:
    """The rules for FACTS, and how many values each category left out.

    A repository's name that is too common still matches as owner/name, and
    counts as left out of the bare match.
    """
    net = facts.tailnet
    rules, left = _repos(facts)
    omitted = {"private-repo": left}
    rules += [Rule("tailnet-suffix", _literal(net.suffix))]
    rules += [Rule("tailnet-name", _literal(net.name))]
    found, omitted["tailnet-host"] = _named("tailnet-host", net.hosts, facts.words)
    rules += found
    every = stems(net.hosts)
    kept = [s for s in every if not _common(s, facts.words)]
    omitted["tailnet-stem"] = len(every) - len(kept)
    rules += [Rule("tailnet-stem", f"(?i){_bound(s, 0)}{re.escape(s)}") for s in kept]
    rules += _ipv4(
        "tailnet-ipv4",
        rf"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])(?:\.{_OCTET}){{2}}",
    )
    rules += [Rule("tailnet-ipv6", r"(?i)\bfd7a:115c:a1e0:[0-9a-f:]*")]
    rules += [
        Rule("home-path", f"/(?:Users|home)/{re.escape(name)}{_bound(name, -1)}")
        for name in sorted({*facts.operators, AGENT})
    ]
    if facts.checkout is not None:
        root = str(facts.checkout)
        regex = f"{_PATH}/{re.escape(root)}{_bound(root, -1)}"
        rules += [Rule("checkout-path", regex, (GENERIC_HOME,))]
    tools = "|".join(map(re.escape, TOOL_DIRS))
    # A regex naming directories, not a path to a credential, which gitleaks
    # flags for its .config.
    dirs = r"/(?:\.config|\.cache|\.local/(?:share|state))/"  # gitleaks:allow
    regex = rf"{_PATH}{dirs}(?:{tools})\b"
    rules += [Rule("tool-dir", regex, (GENERIC_HOME,))]
    rules += _ipv4(
        "private-ipv4",
        rf"10(?:\.{_OCTET}){{3}}",
        rf"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.{_OCTET}){{2}}",
        rf"192\.168(?:\.{_OCTET}){{2}}",
    )
    rules += [
        Rule("local-name", r"(?i)\b[a-z0-9][a-z0-9-]*\.(?:local|lan)(?:[^\w(.-]|$)"),
        Rule(
            "mac-address",
            r"(?i)\b[0-9a-f]{2}(?:(?::[0-9a-f]{2}){5}|(?:-[0-9a-f]{2}){5})\b",
        ),
        Rule("op-reference", r"""op://[^\s"'`<>]+"""),
    ]
    if "session_sockets" in (userconfig.table("gitleaks") or {}):
        sockets = userconfig.strings("gitleaks", "session_sockets")
        rules += [Rule("session-socket", re.escape(path)) for path in sockets]
    found, omitted["account-name"] = _named(
        "account-name", facts.operators, facts.words
    )
    rules += [*found, Rule("op-account", _literal(setting("op", "account")))]
    for category, values in (
        ("op-vault", facts.vaults),
        ("op-item", facts.items),
        ("endpoint", facts.endpoints),
        ("keychain", facts.names),
    ):
        found, omitted[category] = _named(category, values, facts.words)
        rules += found
    return rules, omitted


def _rule_lines(rule_id: str, description: str, regex: str) -> list[str]:
    return [
        "[[rules]]",
        f"id = {json.dumps(rule_id)}",
        f"description = {json.dumps(description)}",
        f"regex = {json.dumps(regex)}",
        "",
    ]


def render_gitleaks(rules: Iterable[Rule]) -> str:
    """The config: the committed one, extended by the canary and RULES."""
    lines = [
        "# Generated by `tbhbdev host bootstrap`, which replaces any edit.",
        "",
        "[extend]",
        f"path = {json.dumps(GITLEAKS_EXTENDS)}",
        "",
        *_rule_lines(
            GITLEAKS_CANARY, "The canary: the private config is in use", CANARY_REGEX
        ),
    ]
    counts: dict[str, int] = {}
    for rule in rules:
        counts[rule.category] = counts.get(rule.category, 0) + 1
        rule_id = f"{rule.category}-{counts[rule.category]}"
        lines += _rule_lines(rule_id, GITLEAKS_RULES[rule.category], rule.regex)
        if rule.allow:
            lines += ["[[rules.allowlists]]", f"regexes = {json.dumps(rule.allow)}", ""]
    return "\n".join(lines)


def op_stdin(stdin: str, *args: str) -> None:
    """Run op with STDIN, which may hold a secret, so no message quotes op."""
    done = _run(["op", *args, "--account", setting("op", "account")], stdin=stdin)
    if done.returncode != 0:
        message = f"op {shlex.join(args)} failed: exit {done.returncode}"
        raise HostError(message)


def publish_gitleaks(text: str) -> Result:
    """Put TEXT in the gitleaks_ref() field, creating the item when it's missing."""
    vault, title, label = secrets.parse_ref(gitleaks_ref())
    items = op_list("item", "list", "--vault", vault)
    found = sum(_str(i, "title") == title for i in items)
    if found > 1:
        message = f"{vault} holds {found} items named {title}"
        raise HostError(message)
    field: Json = {"id": label, "label": label, "type": "CONCEALED", "value": text}
    if not found:
        item: Json = {"title": title, "category": "SECURE_NOTE", "fields": [field]}
        op_stdin(json.dumps(item), "item", "create", "-", "--vault", vault)
        return Result(CHANGED, f"created {gitleaks_ref()}")
    source = f"op item get {title}"
    got = op("item", "get", title, "--vault", vault, "--reveal", "--format", "json")
    entries = _dicts([_json(got, source)])
    if not entries:
        message = f"{source} printed something other than an object"
        raise HostError(message)
    fields = _dicts(entries[0].get("fields"))
    if [_str(f, "value") for f in fields if _str(f, "label") == label] == [text]:
        return Result(OK, f"{gitleaks_ref()} is current")
    entries[0]["fields"] = [f for f in fields if _str(f, "label") != label] + [field]
    op_stdin(json.dumps(entries[0]), "item", "edit", title, "--vault", vault)
    return Result(CHANGED, f"updated {gitleaks_ref()}")


def _generated(
    operators: Iterable[str], ws: Workspace | None, urls: list[str]
) -> tuple[str, list[Rule], dict[str, int]]:
    rules, omitted = gitleaks_rules(gitleaks_facts(operators, ws, urls))
    text = render_gitleaks(rules)
    problem = gitleaks_problem(text)
    if problem is not None:
        raise HostError(problem)
    return text, rules, omitted


def gitleaks_generate(
    operators: Iterable[str], ws: Workspace | None, urls: list[str]
) -> None:
    """Generate the private gitleaks config and publish it to gitleaks_ref()."""
    try:
        text, rules, omitted = _generated(operators, ws, urls)
        result = publish_gitleaks(text)
    except HostError as exc:
        message = f"the private gitleaks config: {exc}; wrote nothing"
        raise HostError(message) from exc
    for category in GITLEAKS_RULES:
        count = sum(r.category == category for r in rules)
        left = omitted.get(category)
        tail = f", {left} too common or short to match alone" if left else ""
        print(f"gitleaks-generate: {category}: {count} rule(s){tail}")
    print(f"gitleaks-generate: {result.status}: {result.message}")


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


NO_WORKSPACE = (
    "the user config has no [workspace]; skipping workspace access, clones and tools"
)


def _supported(chosen: list[Host], failed: list[str]) -> list[Host]:
    """The hosts in CHOSEN with a known OS. The others go in FAILED."""
    kept: list[Host] = []
    for host in chosen:
        try:
            platform_for(host)
        except HostError as exc:
            _report(host, exc)
            failed.append(host.name)
            continue
        kept.append(host)
    return kept


def _operators(known: dict[str, str]) -> list[str] | None:
    """The operator's name on every tagged host, or None if one can't be found.

    KNOWN maps host names to the names already found; the other hosts are asked.
    """
    found: list[str] = []
    for host in hosts(host_tag()):
        if host.name in known:
            found.append(known[host.name])
            continue
        try:
            found.append(identity(host)[0])
        except HostError as exc:
            _report(host, exc)
            print("keeping the published private gitleaks config")
            return None
    return found


def cmd_bootstrap(args: argparse.Namespace) -> int:
    touch_id("bootstrap")
    named = _check_vaults(args.vaults)
    failed: list[str] = []
    chosen = _supported(select(args.host, every=args.all), failed)
    urls = endpoints()
    copy = agent_copy(urls)
    ws = workspace()
    server = ensure_server()
    ensure_vaults({*named, *(v for h in chosen for v in base_vaults(h))})
    account = Account.load(server)
    plans = _plans(account, chosen, lambda held: held | named)
    account.grant(v for vaults in plans.values() for v in vaults)
    if ws is None:
        print(NO_WORKSPACE)
    ready: list[Host] = []
    operators: dict[str, str] = {}
    for host in chosen:
        try:
            operators[host.name] = set_up(host, ws, repair=args.repair_acl)
            ensure_keychain(host)
            print(f"{host.name}: config.toml {agent_config(host, 'install', copy)}")
            refresh(account, host, plans[host], urls)
        except HostError as exc:
            _report(host, exc)
            failed.append(host.name)
            continue
        ready.append(host)
    # The operator's names come from the hosts, so with none ready, or one
    # whose operator is unknown, the config would lose them: keep the one in
    # 1Password instead.
    every = _operators(operators) if ready else None
    if every is not None:
        gitleaks_generate(every, ws, urls)
    failed += upgrade(ready, ws, urls)
    if failed:
        message = f"bootstrap failed on {', '.join(failed)}"
        raise HostError(message)
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    chosen = select(args.host, every=args.all)
    urls = endpoints()
    ws = workspace()
    if ws is None:
        print(NO_WORKSPACE)
    failed = upgrade(chosen, ws, urls)
    if failed:
        message = f"upgrade failed on {', '.join(failed)}"
        raise HostError(message)
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    touch_id("grant")
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
    touch_id("revoke")
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
    ws = workspace()
    server = find_server()
    account = Account.load(server) if server is not None else None
    for host in chosen:
        token = account.current(host) if account is not None else None
        entry = "absent" if keychain_held(host) is None else "present"
        tail = (
            f"keychain entry {entry}; config.toml {agent_config(host, 'check', copy)}"
        )
        if token is None:
            print(f"{host.name}: no token; {tail}")
        else:
            print(
                f"{host.name}: token {token.name}; "
                f"vaults {', '.join(sorted(token.vaults))}; "
                f"expires {token.expires:%Y-%m-%dT%H:%M:%SZ}; {tail}"
            )
        print(f"{host.name}: workspace access {access_state(host, ws)}")
    return 0


def touch_id(command: str) -> None:
    """Pass only when the person at this Mac passes a Touch ID check.

    This is the guard that keeps an agent from running COMMAND, one of
    bootstrap, grant, revoke and `exec --operator`. It has no bypass.
    """
    if platform.system() != "Darwin":
        message = f"{command} needs macOS with Touch ID"
        raise HostError(message)
    done = _run([SWIFT, "-"], stdin=TOUCH_ID)
    if done.returncode == TOUCH_ID_UNAVAILABLE:
        message = f"{command} needs Touch ID, which isn't available here"
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
        touch_id("--operator")
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

    made: dict[str, argparse.ArgumentParser] = {}
    for name, run, nargs, text in (
        ("bootstrap", cmd_bootstrap, "*", "set up hosts as root, then upgrade them"),
        ("grant", cmd_grant, "+", "add vaults to hosts' tokens"),
        ("revoke", cmd_revoke, "+", "take vaults from hosts' tokens"),
    ):
        command = sub.add_parser(name, help=text)
        _targets(command)
        command.add_argument("vaults", nargs=nargs, metavar="VAULT")
        command.set_defaults(run=run)
        made[name] = command
    made["bootstrap"].add_argument(
        "--repair-acl",
        action="store_true",
        help="reapply the workspace ACL to everything below the root",
    )

    for name, run, text in (
        ("upgrade", cmd_upgrade, "bring hosts up to date, unattended"),
        ("status", cmd_status, "show hosts' tokens, configs and workspace access"),
    ):
        command = sub.add_parser(name, help=text)
        _targets(command)
        command.set_defaults(run=run)

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
