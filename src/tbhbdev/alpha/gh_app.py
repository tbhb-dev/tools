"""Mint a GitHub App installation token with secrets read through Connect.

    tbhbdev alpha gh-app APP OWNER/REPO [--fresh] [--dry-run] [--all-repos]
    tbhbdev alpha gh-app APP --cooldown-until EPOCH
    tbhbdev alpha gh-app APP --cooldown-status

A mint reads the App's `app_id` and `private_key` from the `github-app-APP` item
through `secrets.Store`, signs a JWT with `openssl`, and exchanges it for an
installation token. It writes a `hosts.yml` in gh's layout under
`$XDG_STATE_HOME/gh-app` and prints that directory. Nothing here reads a
service account or runs `op`. The token, the key and the JWT never reach
stdout, stderr, an exception message or a repr.

The functions down to `Env` are pure. `Env` holds the seams the rest uses: the
clock, the state root, the secret store, the openssl runner and the HTTP
transport.
"""

import argparse
import base64
import json
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, override

import httpx

from tbhbdev import secrets

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

type Json = dict[str, object]

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_COOLDOWN = 75
APP_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")
REPO_PATTERN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
# Eleven digits reach the year 5138, which datetime still formats.
EPOCH_PATTERN = re.compile(r"[0-9]{1,11}", re.ASCII)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.~+/=-]+")
PEM_PATTERN = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL
)
DEFAULT_VAULT = "tbhb-dev"
API = "https://api.github.com"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
REUSE_MARGIN = 600
JWT_BACKDATE = 60
JWT_LIFETIME = 540
PEM_WIDTH = 64
# mutmut changes only function bodies, so these file names, which mint and
# store_token share and the filesystem may match in any case, are constants.
HOSTS_FILE = "hosts.yml"
SLUG_FILE = "app-slug"
EXPIRES_FILE = "expires-at"
# ruff wants `check` written out; False is subprocess.run's default.
NO_CHECK = False
HEADER = {"alg": "RS256", "typ": "JWT"}
USAGE = """usage: {prog} APP OWNER/REPO [--fresh] [--dry-run] [--all-repos] [--vault V]
       {prog} APP --cooldown-until EPOCH
       {prog} APP --cooldown-status
"""


class GhAppError(Exception):
    """A mint failed. The message never holds a token, a key or a JWT."""


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


@dataclass(frozen=True)
class Minted:
    token: str = field(repr=False)
    expires_at: int


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
        if ns.repo is not None or ns.fresh or ns.dry_run or ns.all_repos:
            message = "a cooldown option takes only the app"
            raise UsageError(message)
    elif ns.repo is None:
        message = "OWNER/REPO is required"
        raise UsageError(message)
    elif not REPO_PATTERN.fullmatch(ns.repo) or ".." in ns.repo.split("/"):
        message = f"repository {ns.repo!r} must be OWNER/REPO"
        raise UsageError(message)
    return Args(
        ns.app,
        ns.repo or "",
        ns.fresh,
        ns.dry_run,
        ns.all_repos,
        ns.vault,
        until,
        ns.cooldown_status,
    )


def state_root(environ: dict[str, str], home: Path) -> Path:
    base = environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / "gh-app"


def split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def out_dir(state: Path, app: str, repo: str, *, all_repos: bool) -> Path:
    owner, name = split_repo(repo)
    return state / app / owner / ("_all-repos" if all_repos else name)


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


def rebuild_pem(raw: str) -> str:
    """A PEM with proper lines from RAW, which a concealed field may flatten.

    Text that holds no BEGIN and END pair comes back unchanged, for openssl to
    refuse.
    """
    found = PEM_PATTERN.search(raw)
    if found is None:
        return raw
    label, body = found.groups()
    body = "".join(body.split())
    lines = [body[i : i + PEM_WIDTH] for i in range(0, len(body), PEM_WIDTH)]
    return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----\n"])


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims(app_id: str, now: int) -> Json:
    return {"iat": now - JWT_BACKDATE, "exp": now + JWT_LIFETIME, "iss": app_id}


def signing_input(app_id: str, now: int) -> bytes:
    parts = [
        b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in (HEADER, claims(app_id, now))
    ]
    return ".".join(parts).encode()


def lookup_paths(repo: str) -> list[str]:
    owner, _ = split_repo(repo)
    return [f"/repos/{repo}/installation", f"/orgs/{owner}/installation"]


def token_body(repo: str, *, all_repos: bool) -> Json:
    return {} if all_repos else {"repositories": [split_repo(repo)[1]]}


def json_object(response: httpx.Response) -> Json:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def installation_id(response: httpx.Response) -> int | None:
    found = json_object(response).get("id")
    if response.status_code != httpx.codes.OK or not isinstance(found, int):
        return None
    return found


def expiry(value: object) -> int | None:
    """The epoch an ISO 8601 VALUE with a zone names, or None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if moment.tzinfo is None else int(moment.timestamp())


def parse_token(payload: Json) -> Minted:
    """The token and its expiry in GitHub's reply.

    Raises:
        GhAppError: The reply lacks either. The message quotes GitHub's own
            `message` field and nothing else.
    """
    token = payload.get("token")
    epoch = expiry(payload.get("expires_at"))
    if isinstance(token, str) and TOKEN_PATTERN.fullmatch(token) and epoch is not None:
        return Minted(token, epoch)
    reason = payload.get("message")
    detail = reason if isinstance(reason, str) and reason else "no message"
    message = f"GitHub did not return a token: {detail}"
    raise GhAppError(message)


def render_hosts(app: str, token: str) -> str:
    user = f'"{app}[bot]"'
    return (
        "github.com:\n"
        "    users:\n"
        f"        {user}:\n"
        f"            oauth_token: {token}\n"
        "    git_protocol: https\n"
        f"    oauth_token: {token}\n"
        f"    user: {user}\n"
    )


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
            ["openssl", *argv], input=data, capture_output=True, check=NO_CHECK
        )
    except FileNotFoundError:
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
    except BaseException:
        Path(temp).unlink()
        raise


def fill(handle: int, text: str) -> None:
    with os.fdopen(handle, "wb") as stream:
        stream.write(text.encode())


def make_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def read_field(env: Env, args: Args, name: str) -> str:
    item = f"github-app-{args.app}"
    try:
        value = env.store().read(f"op://{args.vault}/{item}/{name}")
    except secrets.SecretsError:
        value = ""
    if not value.strip():
        message = f"cannot read {name} from {args.vault}/{item}"
        raise GhAppError(message)
    return value


def sign(env: Env, app_id: str, key: str) -> str:
    """A signed JWT for APP_ID, from the PEM KEY, which only a 0600 file holds."""
    with tempfile.TemporaryDirectory() as directory:
        # mkstemp creates the file 0600, before anything is written to it.
        handle, keyfile = tempfile.mkstemp(dir=directory)
        fill(handle, key)
        if env.openssl(["pkey", "-in", keyfile, "-noout"]).returncode:
            message = "private_key does not parse"
            raise GhAppError(message)
        data = signing_input(app_id, env.now())
        done = env.openssl(["dgst", "-sha256", "-sign", keyfile], data)
    if done.returncode or not done.stdout:
        message = "cannot sign the JWT"
        raise GhAppError(message)
    return f"{data.decode()}.{b64url(done.stdout)}"


def find_installation(client: httpx.Client, repo: str) -> int | None:
    """The installation of the repository, or else of its owner."""
    for path in lookup_paths(repo):
        ident = installation_id(client.get(path))
        if ident is not None:
            return ident
    return None


def request_token(env: Env, args: Args, jwt: str) -> Minted:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {jwt}",
    }
    try:
        with httpx.Client(
            base_url=API, headers=headers, timeout=TIMEOUT, transport=env.transport
        ) as client:
            ident = find_installation(client, args.repo)
            if ident is None:
                owner, _ = split_repo(args.repo)
                message = (
                    f"app {args.app} has no installation for {args.repo} or {owner}"
                )
                raise GhAppError(message)
            reply = client.post(
                f"/app/installations/{ident}/access_tokens",
                json=token_body(args.repo, all_repos=args.all_repos),
            )
    except httpx.HTTPError as exc:
        message = f"the GitHub request failed: {type(exc).__name__}"
        raise GhAppError(message) from None
    return parse_token(json_object(reply))


def store_token(args: Args, out: Path, minted: Minted) -> None:
    make_dir(out)
    write_file(out / HOSTS_FILE, render_hosts(args.app, minted.token))
    write_file(out / SLUG_FILE, f"{args.app}\n")
    # Last, so that no reader sees a new expiry beside an old token.
    expires = 0 if args.fresh else minted.expires_at
    write_file(out / EXPIRES_FILE, f"{expires}\n")


def mint(args: Args, env: Env) -> int:
    out = out_dir(env.state, args.app, args.repo, all_repos=args.all_repos)
    now = env.now()
    until = cooldown_until(read_text(cooldown_path(env.state, args.app)), now)
    if until is not None:
        sys.stderr.write(f"{cooldown_message(until)}\n")
        return EXIT_COOLDOWN
    if (
        not args.dry_run
        and (out / HOSTS_FILE).is_file()
        and reusable(read_text(out / EXPIRES_FILE), now, fresh=args.fresh)
    ):
        sys.stdout.write(f"{out}\n")
        return 0
    app_id = read_field(env, args, "app_id").strip()
    key = rebuild_pem(read_field(env, args, "private_key"))
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
    try:
        return dispatch(args, env or default_env())
    except GhAppError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_FAILURE
    except OSError as exc:
        # An exception raised while writing state holds a path and a reason,
        # never a value.
        sys.stderr.write(f"cannot write {exc.filename}: {exc.strerror}\n")
        return EXIT_FAILURE
