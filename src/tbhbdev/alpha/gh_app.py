"""Mint a GitHub App installation token with secrets read through Connect.

    tbhbdev alpha gh-app APP OWNER/REPO [--fresh] [--dry-run] [--all-repos]
    tbhbdev alpha gh-app APP --cooldown-until EPOCH
    tbhbdev alpha gh-app APP --cooldown-status

`mint_token` returns a repository and permission restricted token signed in memory.

A mint reads the App's `app_id` and `private_key` from the `github-app-APP` item
through one `secrets.Store`, checks that the key is an RSA key, signs a JWT
with `openssl`, and exchanges it for an installation token. It writes a
`hosts.yml` in gh's layout under
`$XDG_STATE_HOME/gh-app/VAULT/APP/OWNER/REPO`, or `.../OWNER/+all` with
`--all-repos`, and prints that directory. No GitHub repository can be named
`+all`, and each vault has its own tree, so no two requests share a token. A
cached token with more than ten minutes left is reused, even during a
cooldown, because reuse calls neither GitHub nor the vault. A cooldown
refuses every other mint. Each file is synced to disk, then renamed into
place, and the directory is synced after. The installation lookup falls
through from the repository to its owner on a 404 only, and any other
refusal is an error. Nothing here reads a service account or runs `op`. The
token, the key and the JWT never reach stdout, stderr, an exception message or
a repr.

`Env` supplies the clock, state root, secret store, OpenSSL runner and HTTP
transport. The `decisions.gh_app` module builds requests and interprets replies.
"""

import argparse
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, override

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tbhbdev import secrets
from tbhbdev.alpha.decisions import gh_app as decisions
lazy from tbhbdev.alpha.agent import host_tools
lazy from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

# Keep the original import paths for callers.
HEADER = decisions.HEADER
JWT_BACKDATE = decisions.JWT_BACKDATE
JWT_LIFETIME = decisions.JWT_LIFETIME
PEM_PATTERN = decisions.PEM_PATTERN
PEM_WIDTH = decisions.PEM_WIDTH
RSA_HEADER = decisions.RSA_HEADER
RSA_MODULUS = decisions.RSA_MODULUS
SECRET_LIKE = decisions.SECRET_LIKE
TOKEN_PATTERN = decisions.TOKEN_PATTERN
CachedInstallationMissingError = decisions.CachedInstallationMissingError
GhAppError = decisions.GhAppError
InstallationMissingError = decisions.InstallationMissingError
Json = decisions.Json
Minted = decisions.Minted
b64url = decisions.b64url
claims = decisions.claims
detail = decisions.detail
expiry = decisions.expiry
is_rsa = decisions.is_rsa
lookup_paths = decisions.lookup_paths
parse_token = decisions.parse_token
rebuild_pem = decisions.rebuild_pem
render_hosts = decisions.render_hosts
signing_input = decisions.signing_input
split_repo = decisions.split_repo
token_body = decisions.token_body

EXIT_FAILURE = 1
RUN_ENV = "TBHBDEV_AGENT_RUN"  # Set by exec in every run's environment.
EXIT_USAGE = 2
EXIT_COOLDOWN = 75
APP_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")
REPO_PATTERN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
PERMISSION_PATTERN = re.compile(r"[a-z][a-z_]*:(?:read|write)", re.ASCII)
# GitHub's repository permission names, so a typo fails before any network call.
PERMISSION_NAMES = frozenset(
    {
        "actions",
        "administration",
        "checks",
        "codespaces",
        "contents",
        "dependabot_secrets",
        "deployments",
        "discussions",
        "environments",
        "issues",
        "merge_queues",
        "metadata",
        "packages",
        "pages",
        "pull_requests",
        "repository_advisories",
        "repository_hooks",
        "repository_projects",
        "secret_scanning_alerts",
        "secrets",
        "security_events",
        "single_file",
        "statuses",
        "vulnerability_alerts",
        "workflows",
    }
)
# Eleven digits reach the year 5138, which datetime still formats.
EPOCH_PATTERN = re.compile(r"[0-9]{1,11}", re.ASCII)
DEFAULT_VAULT = "tbhb-dev"
API = "https://api.github.com"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REUSE_MARGIN = 600
ALL_REPOS_DIR = "+all"
NOT_A_PART = (".", "..")
# mutmut changes only function bodies, so these file names, which mint and
# store_token share and the filesystem may match in any case, are constants.
HOSTS_FILE = "hosts.yml"
SLUG_FILE = "app-slug"
EXPIRES_FILE = "expires-at"
# ruff wants `check` written out; False is subprocess.run's default.
# cspell:words RDONLY
NO_CHECK = False
MINT_FAILURE = "GitHub App token mint failed"
USAGE = """usage: {prog} APP OWNER/REPO [--fresh] [--dry-run] [--all-repos] [--vault V]
                                 [--permissions NAME:LEVEL,...]
       {prog} APP --cooldown-until EPOCH
       {prog} APP --cooldown-status
"""


class UsageError(Exception):
    """The command line is wrong."""


class HelpRequestedError(Exception):
    """The command line asked for the usage text."""


class Reader(Protocol):
    def read(self, ref: str, /) -> str: ...


class OpenSsl(Protocol):
    def __call__(
        self, argv: list[str], data: bytes | None = None, /
    ) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True)
class Args:
    app: str
    repo: str  # empty for a cooldown option
    fresh: bool
    dry_run: bool
    all_repos: bool
    vault: str
    cooldown_until: int | None
    cooldown_status: bool
    permissions: tuple[tuple[str, str], ...] = ()


class Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)

    @override
    def print_help(self, *_args: object, **_kwargs: object) -> None:
        raise HelpRequestedError


def parse_epoch(text: str | None) -> int | None:
    """The epoch TEXT holds, or None when it isn't whole seconds."""
    if text is None:
        return None
    stripped = text.strip()
    if not EPOCH_PATTERN.fullmatch(stripped):
        return None
    return int(stripped)


def parse_args(argv: Sequence[str]) -> Args:
    """Parse ARGV.

    Raises:
        HelpRequestedError: ARGV holds -h or --help.
        UsageError: An argument or a combination of them is wrong.
    """
    parser = Parser()
    parser.add_argument("app")
    parser.add_argument("repo", nargs="?")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all-repos", action="store_true")
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--permissions", action="append")
    parser.add_argument("--cooldown-until")
    parser.add_argument("--cooldown-status", action="store_true")
    ns = parser.parse_args(argv)
    if not APP_PATTERN.fullmatch(ns.app):
        message = f"app {ns.app!r} must match {APP_PATTERN.pattern}"
        raise UsageError(message)
    until = None
    if ns.cooldown_until is not None:
        until = parse_epoch(ns.cooldown_until)
        if until is None:
            message = "--cooldown-until needs a whole number of seconds since the epoch"
            raise UsageError(message)
    if until is not None and ns.cooldown_status:
        message = "--cooldown-until and --cooldown-status are exclusive"
        raise UsageError(message)
    if until is not None or ns.cooldown_status:
        if (
            ns.repo is not None
            or ns.fresh
            or ns.dry_run
            or ns.all_repos
            or ns.permissions is not None
        ):
            message = "a cooldown option takes only the app"
            raise UsageError(message)
    elif ns.repo is None:
        message = "OWNER/REPO is required"
        raise UsageError(message)
    elif not valid_repo(ns.repo):
        message = f"repository {ns.repo!r} must be OWNER/REPO"
        raise UsageError(message)
    if not valid_vault(ns.vault):
        message = f"vault {ns.vault!r} cannot be a directory name"
        raise UsageError(message)
    permissions = parse_permissions(ns.permissions)
    return Args(
        ns.app,
        ns.repo or "",
        ns.fresh,
        ns.dry_run,
        ns.all_repos,
        ns.vault,
        until,
        ns.cooldown_status,
        permissions,
    )


def parse_permissions(values: list[str] | None) -> tuple[tuple[str, str], ...]:
    """NAME:LEVEL pairs from the option's one comma list, sorted; empty for none.

    Raises:
        UsageError: The option repeats, an entry isn't NAME:read or NAME:write
            with a known name, or a name repeats.
    """
    if values is None:
        return ()
    if len(values) > 1:
        message = "--permissions can be given once"
        raise UsageError(message)
    text = values[0]
    pairs: dict[str, str] = {}
    for entry in text.split(","):
        if (
            PERMISSION_PATTERN.fullmatch(entry) is None
            or entry.partition(":")[0] not in PERMISSION_NAMES
            or entry.partition(":")[0] in pairs
        ):
            message = (
                f"--permissions entry {entry!r} must be a known NAME:read "
                "or NAME:write, once"
            )
            raise UsageError(message)
        name, _, level = entry.partition(":")
        pairs[name] = level
    return tuple(sorted(pairs.items()))


def valid_repo(repo: str) -> bool:
    """Whether REPO is OWNER/REPO, with no part that climbs or stays in a path."""
    parts = repo.split("/")
    return REPO_PATTERN.fullmatch(repo) is not None and not set(parts) & set(NOT_A_PART)


def valid_vault(vault: str) -> bool:
    """Whether VAULT can be one component of a path."""
    return vault not in ("", *NOT_A_PART) and "/" not in vault and "\0" not in vault


def state_root(environ: dict[str, str], home: Path) -> Path:
    base = environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / "gh-app"


def out_dir(state: Path, args: Args) -> Path:
    owner, name = split_repo(args.repo)
    # A narrowed token has its own directory: `+` can't appear in a repository name.
    narrowed = "".join(f"+{n.replace('_', '-')}-{v}" for n, v in args.permissions)
    return (
        state
        / args.vault
        / args.app
        / owner
        / ((ALL_REPOS_DIR if args.all_repos else name) + narrowed)
    )


def cooldown_path(state: Path, app: str) -> Path:
    return state / "cooldown" / app


def cooldown_until(text: str | None, now: int) -> int | None:
    """The epoch an active cooldown lasts until, or None when there isn't one."""
    until = parse_epoch(text)
    if until is None or until <= now:
        return None
    return until


def reusable(expires: str | None, now: int, *, fresh: bool) -> bool:
    if fresh:
        return False
    epoch = parse_epoch(expires)
    return epoch is not None and epoch - now > REUSE_MARGIN


def utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cooldown_message(until: int) -> str:
    return f"cooldown until {utc(until)}"


def json_object(response: httpx.Response) -> Json:
    return decisions.json_object(response.content)


def installation_id(response: httpx.Response) -> int | None:
    return decisions.installation_id(response.status_code, json_object(response))


@dataclass(frozen=True)
class Env:
    state: Path
    now: Callable[[], int]
    store: Callable[[], Reader]
    openssl: OpenSsl
    transport: httpx.BaseTransport | None = None


def run_openssl(
    argv: list[str], data: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # nosec B603 B607
            [host_tools.resolve("openssl"), *argv],
            input=data,
            capture_output=True,
            check=NO_CHECK,
        )
    except FileNotFoundError, Invalid:
        message = "openssl isn't installed"
        raise GhAppError(message) from None


def clock() -> int:
    return int(time.time())


def default_env() -> Env:
    return Env(
        state_root(dict(os.environ), Path.home()),
        clock,
        secrets.Store,
        run_openssl,
    )


def read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode()
    except OSError, UnicodeDecodeError:
        return None


def write_file(path: Path, text: str) -> None:
    """Write TEXT to PATH, owner-only, so that no reader sees it half-written."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        fill(handle, text)
        Path(temp).replace(path)
        sync_dir(path.parent)
    except BaseException:
        # After the rename the temp file is gone, and a failed cleanup must not
        # hide the error that brought us here.
        Path(temp).unlink(missing_ok=True)
        raise


def fill(handle: int, text: str) -> None:
    with os.fdopen(handle, "wb") as stream:
        stream.write(text.encode())
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def unreadable(args: Args, name: str) -> GhAppError:
    return GhAppError(f"cannot read {name} from {args.vault}/github-app-{args.app}")


def read_field(store: Reader, args: Args, name: str) -> str:
    try:
        value = store.read(f"op://{args.vault}/github-app-{args.app}/{name}")
    except secrets.SecretsError:
        value = ""
    if not value.strip():
        raise unreadable(args, name)
    return value


def read_fields(env: Env, args: Args) -> tuple[str, str]:
    """The App's id and its PEM key, both read through one store."""
    try:
        store = env.store()
    except secrets.SecretsError:
        raise unreadable(args, "app_id") from None
    app_id = read_field(store, args, "app_id").strip()
    return app_id, rebuild_pem(read_field(store, args, "private_key"))


def sign(env: Env, app_id: str, key: str) -> str:
    """A signed JWT for APP_ID, from the PEM KEY, which only a 0600 file holds."""
    with tempfile.TemporaryDirectory() as directory:
        # mkstemp creates the file 0600, before anything is written to it.
        handle, keyfile = tempfile.mkstemp(dir=directory)
        fill(handle, key)
        described = env.openssl(["pkey", "-in", keyfile, "-noout", "-text"])
        if described.returncode:
            message = "private_key does not parse"
            raise GhAppError(message)
        if not is_rsa(described.stdout):
            message = "the app private key is not an RSA key"
            raise GhAppError(message)
        data = signing_input(app_id, env.now())
        done = env.openssl(["dgst", "-sha256", "-sign", keyfile], data)
    if done.returncode or not done.stdout:
        message = "cannot sign the JWT"
        raise GhAppError(message)
    return f"{data.decode()}.{b64url(done.stdout)}"


def find_installation(client: httpx.Client, repo: str) -> int | None:
    """The installation of the repository, or else of its owner.

    Only a 404 moves on to the owner.

    Raises:
        GhAppError: GitHub answered a lookup with anything else but an id.
    """
    for path in lookup_paths(repo):
        response = client.get(path)
        ident = decisions.lookup_result(response.status_code, response.content)
        if ident is not None:
            return ident
    return None


def exchange_token(  # noqa: PLR0913  # Optional installation cache seams.
    env: Env,
    repository: str,
    jwt: str,
    body: Json,
    *,
    known_installation: int | None = None,
    installation_found: Callable[[int], None] | None = None,
) -> Minted:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {jwt}",
    }
    try:
        with httpx.Client(
            base_url=API,
            headers=headers,
            timeout=TIMEOUT,
            transport=env.transport,
            trust_env=False,
        ) as client:
            ident = known_installation
            if ident is None:
                ident = find_installation(client, repository)
            if ident is None:
                raise InstallationMissingError
            if installation_found is not None:
                installation_found(ident)
            reply = client.post(
                f"/app/installations/{ident}/access_tokens",
                json=body,
            )
    except httpx.HTTPError as exc:
        message = f"the GitHub request failed: {type(exc).__name__}"
        raise GhAppError(message) from None
    return decisions.exchange_result(
        reply.status_code, reply.content, cached=known_installation is not None
    )


def request_token(env: Env, args: Args, jwt: str) -> Minted:
    try:
        return exchange_token(
            env,
            args.repo,
            jwt,
            token_body(
                args.repo, all_repos=args.all_repos, permissions=args.permissions
            ),
        )
    except InstallationMissingError:
        owner, _ = split_repo(args.repo)
        message = f"app {args.app} has no installation for {args.repo} or {owner}"
        raise GhAppError(message) from None


def mint_token(  # noqa: PLR0913  # Explicit credentials and installation cache.
    env: Env,
    *,
    app_id: str,
    pem: str,
    repository: str,
    permissions: Mapping[str, str],
    known_installation: int | None = None,
    installation_found: Callable[[int], None] | None = None,
) -> Minted:
    """Mint a token with explicit scope, keeping the key and token in memory.

    REPOSITORY names OWNER/REPO, or just OWNER for organization permissions.

    Raises:
        GhAppError: Permissions, signing or the token exchange failed.
    """
    decisions.validate_permissions(permissions)
    try:
        key = serialization.load_pem_private_key(
            rebuild_pem(pem).encode(), password=None
        )
    except ValueError, TypeError, UnsupportedAlgorithm:
        message = "private_key does not parse"
        raise GhAppError(message) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        message = "the app private key is not an RSA key"
        raise GhAppError(message)
    data = signing_input(app_id, env.now())
    signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{data.decode()}.{b64url(signature)}"
    body = decisions.scoped_token_body(repository, permissions)
    try:
        return exchange_token(
            env,
            repository,
            jwt,
            body,
            known_installation=known_installation,
            installation_found=installation_found,
        )
    except InstallationMissingError:
        raise InstallationMissingError(MINT_FAILURE) from None
    except CachedInstallationMissingError:
        raise
    except GhAppError:
        # An API error may echo a credential in its message field.
        raise GhAppError(MINT_FAILURE) from None


def store_token(args: Args, out: Path, minted: Minted) -> None:
    make_dir(out)
    write_file(out / HOSTS_FILE, render_hosts(args.app, minted.token))
    write_file(out / SLUG_FILE, f"{args.app}\n")
    # Last, so that no reader sees a new expiry beside an old token.
    expires = 0 if args.fresh else minted.expires_at
    write_file(out / EXPIRES_FILE, f"{expires}\n")


def mint(args: Args, env: Env) -> int:
    out = out_dir(env.state, args)
    now = env.now()
    # Reuse calls neither GitHub nor the vault, so a cooldown doesn't stop it.
    if (
        not args.dry_run
        and (out / HOSTS_FILE).is_file()
        and reusable(read_text(out / EXPIRES_FILE), now, fresh=args.fresh)
    ):
        sys.stdout.write(f"{out}\n")
        return 0
    until = cooldown_until(read_text(cooldown_path(env.state, args.app)), now)
    if until is not None:
        sys.stderr.write(f"{cooldown_message(until)}\n")
        return EXIT_COOLDOWN
    app_id, key = read_fields(env, args)
    jwt = sign(env, app_id, key)
    if args.dry_run:
        sys.stdout.write(
            f"dry run: read app_id and private_key for {args.app}, "
            "signed a JWT and made no request\n"
        )
        return 0
    store_token(args, out, request_token(env, args, jwt))
    sys.stdout.write(f"{out}\n")
    return 0


def set_cooldown(args: Args, until: int, env: Env) -> int:
    path = cooldown_path(env.state, args.app)
    make_dir(path.parent)
    write_file(path, f"{until}\n")
    return 0


def cooldown_status(args: Args, env: Env) -> int:
    until = cooldown_until(read_text(cooldown_path(env.state, args.app)), env.now())
    if until is None:
        sys.stdout.write("no cooldown\n")
        return 0
    sys.stdout.write(f"{cooldown_message(until)}\n")
    return EXIT_COOLDOWN


def dispatch(args: Args, env: Env) -> int:
    if args.cooldown_until is not None:
        return set_cooldown(args, args.cooldown_until, env)
    if args.cooldown_status:
        return cooldown_status(args, env)
    return mint(args, env)


def main(argv: list[str], prog: str, *, env: Env | None = None) -> int:
    """Run the command ARGV names with ENV's seams, which default to the real ones."""
    try:
        args = parse_args(argv)
    except HelpRequestedError:
        sys.stdout.write(USAGE.format(prog=prog))
        return 0
    except UsageError as exc:
        sys.stderr.write(f"{prog}: {exc}\n{USAGE.format(prog=prog)}")
        return EXIT_USAGE
    if os.environ.get(RUN_ENV):
        sys.stderr.write(
            f"{prog}: refused inside an agent run; a run never mints an App token\n"
        )
        return EXIT_FAILURE
    try:
        return dispatch(args, env or default_env())
    except GhAppError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_FAILURE
    except OSError as exc:
        # An exception raised while writing state holds a path and a reason,
        # never a value.
        where = "" if exc.filename is None else f"cannot write {exc.filename}: "
        sys.stderr.write(f"{where}{exc.strerror}\n")
        return EXIT_FAILURE
