"""Build GitHub App requests and interpret replies from plain data."""

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

type Json = dict[str, object]

# A run this long of token characters in a failure message may be a credential.
SECRET_LIKE = re.compile(r"[A-Za-z0-9_.~+/=-]{20,}")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.~+/=-]+")
PEM_PATTERN = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL
)
JWT_BACKDATE = 60
JWT_LIFETIME = 540
PEM_WIDTH = 64
# The text of `openssl pkey -noout -text` for an RSA key. Older OpenSSL builds
# put "RSA " before the header.
RSA_HEADER = re.compile(rb"\A(?:RSA )?Private-Key: \(")
RSA_MODULUS = re.compile(rb"^modulus:", re.MULTILINE)
HEADER = {"alg": "RS256", "typ": "JWT"}


class GhAppError(Exception):
    """A mint failed. The message never holds a token, a key or a JWT."""


class InstallationMissingError(GhAppError):
    """Neither the repository nor its owner has an installation."""


class CachedInstallationMissingError(GhAppError):
    """A supplied installation id returned 404."""


@dataclass(frozen=True)
class Minted:
    token: str = field(repr=False)
    expires_at: int


def split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


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
    if "/" not in repo:
        return [f"/orgs/{repo}/installation"]
    owner, _ = split_repo(repo)
    return [f"/repos/{repo}/installation", f"/orgs/{owner}/installation"]


def token_body(
    repo: str, *, all_repos: bool, permissions: tuple[tuple[str, str], ...] = ()
) -> Json:
    body: Json = {} if all_repos else {"repositories": [split_repo(repo)[1]]}
    if permissions:
        body["permissions"] = dict(permissions)
    return body


def validate_permissions(permissions: Mapping[str, str]) -> None:
    """Require explicit permissions for an in-memory mint."""
    if not permissions or any(
        value not in {"read", "write"} for value in permissions.values()
    ):
        message = "permissions must be nonempty and contain only read or write"
        raise GhAppError(message)


def scoped_token_body(repo: str, permissions: Mapping[str, str]) -> Json:
    body: Json = {"permissions": dict(permissions)}
    if "/" in repo:
        body["repositories"] = [split_repo(repo)[1]]
    return body


def json_object(content: bytes) -> Json:
    try:
        payload = json.loads(content)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def detail(payload: Json) -> str:
    """GitHub's own `message` field, minus anything that looks like a credential."""
    reason = payload.get("message")
    if not isinstance(reason, str) or not reason:
        return "no message"
    return SECRET_LIKE.sub("[redacted]", reason)


def installation_id(status: int, payload: Json) -> int | None:
    found = payload.get("id")
    if status != HTTPStatus.OK or not isinstance(found, int):
        return None
    return found


def lookup_result(status: int, content: bytes) -> int | None:
    """Return an installation id or None when the next lookup should run."""
    if status == HTTPStatus.NOT_FOUND:
        return None
    payload = json_object(content)
    ident = installation_id(status, payload)
    if ident is None:
        message = (
            f"the installation lookup failed with status {status}: {detail(payload)}"
        )
        raise GhAppError(message)
    return ident


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
    message = f"GitHub did not return a token: {detail(payload)}"
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


def exchange_result(status: int, content: bytes, *, cached: bool) -> Minted:
    """Return the token or signal that a cached installation id is stale."""
    if cached and status == HTTPStatus.NOT_FOUND:
        raise CachedInstallationMissingError
    return parse_token(json_object(content))


def is_rsa(text: bytes) -> bool:
    """Whether TEXT, from `openssl pkey -noout -text`, describes an RSA key."""
    return RSA_HEADER.match(text) is not None and RSA_MODULUS.search(text) is not None
