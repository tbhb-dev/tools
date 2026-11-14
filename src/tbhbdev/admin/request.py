"""One policy check, one uncached restricted token, one bounded REST call."""

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, cast

import httpx

from tbhbdev import secrets
from tbhbdev.admin import policy
from tbhbdev.admin.audit import Audit, RefusedError, record
from tbhbdev.alpha import gh_app

MAX_RESPONSE = 1024 * 1024
HTTP_OK = 200
HTTP_NOT_FOUND = 404
AUTHORIZATION = "Authorization"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json",
}
SENSITIVE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    re.DOTALL,
)


@dataclass(frozen=True)
class Settings:
    app: str = "tbhb-dev-admin"
    item: str = "github-app-tbhb-dev-admin"
    vault: str = "tbhb-dev-admin"
    app_id: str = "5733271"


@dataclass(frozen=True)
class Runtime:
    audit: Audit
    env: gh_app.Env = field(repr=False)
    marker: Path | None = None
    settings: Settings = Settings()


def default_runtime() -> Runtime:
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    directory = root / "tbhb/admin-app"
    return Runtime(Audit(directory), gh_app.default_env(), marker=directory / "enabled")


def scope(decision: policy.Decision, method: str) -> tuple[str, dict[str, str]]:
    parts = decision.path.split("/")
    target = "/".join(parts[2:4]) if parts[1] == "repos" else parts[2]
    permission = (
        "metadata"
        if method == "GET" and re.fullmatch(policy.REPO, decision.path)
        else decision.permission
    )
    return target, {permission: "read" if method == "GET" else "write"}


def enabled(marker: Path) -> bool:
    try:
        info = marker.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()


def private_key(runtime: Runtime) -> str:
    """Read the key only on the host that opted in, never with a caller credential.

    ``marker`` is the file that ops creates on the one host that uses the App. A
    runtime without one, as in the tests, always reads.
    """
    settings = runtime.settings
    if runtime.marker is not None and not enabled(runtime.marker):
        msg = "credential_unavailable"
        raise RefusedError(msg)
    try:
        reader: gh_app.Reader = runtime.env.store()
        key = reader.read(f"op://{settings.vault}/{settings.item}/private_key")
    except secrets.SecretsError:
        msg = "credential_unavailable"
        raise RefusedError(msg) from None
    if not key.strip():
        msg = "credential_unavailable"
        raise RefusedError(msg)
    return key


def redact(text: str, token: str, key: str) -> str:
    return SENSITIVE.sub(
        "[redacted]", text.replace(token, "[redacted]").replace(key, "[redacted]")
    )


def send(
    runtime: Runtime, method: str, path: str, body: bytes, decision: policy.Decision
) -> tuple[int, str]:
    target, permissions = scope(decision, method)
    key = private_key(runtime)
    try:
        minted = gh_app.mint_token(
            runtime.env,
            app_id=runtime.settings.app_id,
            pem=key,
            repository=target,
            permissions=permissions,
        )
        with (
            httpx.Client(
                base_url=gh_app.API,
                transport=runtime.env.transport,
                timeout=gh_app.TIMEOUT,
                follow_redirects=False,
                trust_env=False,
                headers={
                    AUTHORIZATION: f"Bearer {minted.token}",
                    **HEADERS,
                },
            ) as client,
            client.stream(method, path, content=body) as response,
        ):
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    msg = "response_too_large"
                    raise RefusedError(msg)
            if response.is_redirect:
                msg = "redirect_refused"
                raise RefusedError(msg)
            return response.status_code, redact(
                data.decode(errors="replace"), minted.token, key
            )
    except gh_app.GhAppError, httpx.HTTPError:
        msg = "request_failed"
        raise RefusedError(msg) from None


class Environment(NamedTuple):
    """What a write to an existing environment could undo."""

    reviewers: frozenset[tuple[str, int]]
    branches: object
    wait_timer: int


def whole_number(value: object) -> int:
    """A JSON integer; a bool, float or string would truncate or convert."""
    if isinstance(value, bool) or not isinstance(value, int):
        # The caller maps any ValueError to a refusal and drops its text.
        raise ValueError(value)  # noqa: TRY004  # pragma: no mutate
    return value


def json_list[T](value: T) -> T:
    """VALUE itself once it is a JSON array; an object or string would be misread."""
    if not isinstance(value, list):
        raise ValueError(value)  # noqa: TRY004  # pragma: no mutate
    return value


def existing_environment(
    runtime: Runtime, path: str, reason: str
) -> Environment | None:
    """The environment's reviewers, branch policy and wait timer; None when new."""
    status, text = call(runtime, "GET", path, None, reason)
    if status == HTTP_NOT_FOUND:
        return None
    if status != HTTP_OK:
        msg = "environment_unreadable"
        raise RefusedError(msg)
    try:
        data = json.loads(text)
        rules = json_list(data["protection_rules"])
        reviewers = frozenset(
            (who["type"], whole_number(who["reviewer"]["id"]))
            for rule in rules
            if rule["type"] == "required_reviewers"
            for who in json_list(rule["reviewers"])
        )
        # A body can't carry wait_timer, so a PUT resets the timer to 0 and an
        # environment without a wait_timer rule has none to lose.
        timer = max(
            (
                whole_number(rule["wait_timer"])
                for rule in rules
                if rule["type"] == "wait_timer"
            ),
            default=0,
        )
        return Environment(reviewers, data["deployment_branch_policy"], timer)
    except ValueError, KeyError, TypeError, RecursionError:
        msg = "environment_unreadable"
        raise RefusedError(msg) from None


def keep_environment(runtime: Runtime, path: str, body: object, reason: str) -> None:
    """A write to an existing environment may not change who approves or where.

    One listed reviewer is enough to authorize a deployment, so adding, dropping
    or swapping one is refused when the environment already has reviewers. A
    current branch policy must be unset (every branch) or the safe one the policy
    requires, because custom branch policies can't be compared with the body.
    A positive wait timer is refused, because the write would reset it to 0.
    The lookup and the write are separate calls, so a change between them is not
    caught.
    """
    current = existing_environment(runtime, path, reason)
    if current is None:
        return
    # The cast string has no runtime effect, so mutating it changes nothing.
    wanted = policy.requested_reviewers(
        cast("dict[str, object]", body)  # pragma: no mutate
    )
    if (
        (current.reviewers and current.reviewers != wanted)
        or (
            current.branches is not None
            and not policy.safe_branch_policy(current.branches)
        )
        or current.wait_timer > 0
    ):
        msg = "protection_weakening"
        raise RefusedError(msg)


def call(  # noqa: PLR0913  # Explicit one-call inputs and optional dry run.
    runtime: Runtime,
    method: str,
    path: str,
    body: object,
    reason: str,
    *,
    dry_run: bool = False,
) -> tuple[int, str]:
    """Enforce policy and audit even when called by another subcommand."""
    decision = policy.check(method, path, body)
    encoded = (
        b""
        if body is None
        else json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    )
    # fold=None survives: None is as falsy as False.
    wire = policy.normalize(path, fold=False)  # pragma: no mutate
    entry = record(method, decision, reason, encoded, wire)
    runtime.audit.append(entry)
    if not decision.allowed:
        raise RefusedError(decision.code)
    if dry_run:
        return 0, ""
    try:
        if method == "PUT" and policy.ENVIRONMENT.fullmatch(decision.path):
            keep_environment(runtime, wire, body, reason)
        status, response = send(runtime, method, wire, encoded, decision)
    except RefusedError as exc:
        runtime.audit.append({**entry, "phase": "after", "code": str(exc)})
        raise
    runtime.audit.append({**entry, "phase": "after", "status": status})
    return status, response
