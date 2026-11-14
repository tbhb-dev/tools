"""One policy check, one uncached restricted token, one bounded REST call."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

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
    settings: Settings = Settings()


def default_runtime() -> Runtime:
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return Runtime(Audit(root / "tbhbdev/admin"), gh_app.default_env())


def scope(decision: policy.Decision, method: str) -> tuple[str, dict[str, str]]:
    parts = decision.path.split("/")
    target = "/".join(parts[2:4]) if parts[1] == "repos" else parts[2]
    permission = (
        "metadata"
        if method == "GET" and re.fullmatch(policy.REPO, decision.path)
        else decision.permission
    )
    return target, {permission: "read" if method == "GET" else "write"}


def private_key(runtime: Runtime) -> str:
    """Use only the service's Connect reader, never a caller credential."""
    settings = runtime.settings
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


def existing_reviewers(
    runtime: Runtime, path: str, reason: str
) -> frozenset[tuple[str, int]]:
    """The environment's current required reviewers; none when it is new."""
    status, text = call(runtime, "GET", path, None, reason)
    if status == HTTP_NOT_FOUND:
        return frozenset()
    if status != HTTP_OK:
        msg = "environment_unreadable"
        raise RefusedError(msg)
    try:
        rules = json.loads(text)["protection_rules"]
        return frozenset(
            (who["type"], who["reviewer"]["id"])
            for rule in rules
            if rule["type"] == "required_reviewers"
            for who in rule["reviewers"]
        )
    except ValueError, KeyError, TypeError:
        msg = "environment_unreadable"
        raise RefusedError(msg) from None


def keep_reviewers(runtime: Runtime, path: str, body: object, reason: str) -> None:
    """An environment write may add reviewers but never drop or swap one.

    Otherwise a different user could replace a security team as the deployment
    authorizer. The lookup and the write are separate calls, so a change between
    them is not caught.
    """
    kept = policy.requested_reviewers(cast("dict[str, object]", body))
    if not existing_reviewers(runtime, path, reason) <= kept:
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
            keep_reviewers(runtime, wire, body, reason)
        status, response = send(runtime, method, wire, encoded, decision)
    except RefusedError as exc:
        runtime.audit.append({**entry, "phase": "after", "code": str(exc)})
        raise
    runtime.audit.append({**entry, "phase": "after", "status": status})
    return status, response
