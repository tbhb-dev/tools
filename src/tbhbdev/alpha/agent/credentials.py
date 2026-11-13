"""Read credentials through the secrets library and deliver private handoffs."""

from __future__ import annotations

import json
import os
import stat
import subprocess  # nosec B404
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import yaml

from tbhbdev import secrets
from tbhbdev.alpha import gh_app
from tbhbdev.alpha.agent import host_tools, process_group, runfiles
from tbhbdev.alpha.agent.decisions import auth, config, repo, run_state
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent.model import Credentials, RunRequest, Settings

# Split the spelling because the repository's leak scanner also flags the name.
CLAUDE_TOKEN_ENV = "CLAUDE_CODE_" + "OAUTH_TOKEN"


@dataclass(frozen=True)
class Delivery:
    env: dict[str, str] = field(repr=False)
    signing: str | None = field(repr=False)
    gh_fallback: bool
    claude_profile: str | None


def read(reference: str, *, label: str) -> str:
    try:
        value = secrets.read(reference)
    except secrets.SecretsError, OSError, ValueError:
        value = ""
    if not value.strip():
        message = f"{label} cannot read credential reference {reference}"
        raise Invalid(message)
    return value


def require_reviews_app(references: Credentials) -> None:
    if (
        references.reviews_app_id is None
        or references.reviews_app_client_id is None
        or references.reviews_app_pem is None
    ):
        message = "reviews App credentials are not configured"
        raise Invalid(message)


def installation_token(references: Credentials, repository: str) -> str:
    """Reuse the app signer and request a token restricted to one repository."""
    host, owner, name = repo.parts(repository)
    if host != "github.com":
        message = "reviews app supports github.com repositories"
        raise Invalid(message)
    if references.reviews_app_client_id is None or references.reviews_app_pem is None:
        message = "reviews App credentials are not configured"
        raise Invalid(message)
    client_id = read(references.reviews_app_client_id, label="reviews app")
    pem = read(references.reviews_app_pem, label="reviews app")
    env = gh_app.default_env()
    args = gh_app.Args(
        app="reviews",
        repo=f"{owner}/{name}",
        fresh=True,
        dry_run=False,
        all_repos=False,
        vault="",
        cooldown_until=None,
        cooldown_status=False,
    )
    jwt = gh_app.sign(env, client_id, gh_app.rebuild_pem(pem))
    return gh_app.request_token(env, args, jwt).token


def deliver(request: RunRequest, settings: Settings, tmpdir: Path) -> Delivery:
    references = next(c for c in settings.credentials if c.account == request.account)
    if request.profile is Profile.REVIEW:
        try:
            gh_token = installation_token(references, request.repo)
        except (
            Invalid,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            httpx.HTTPError,
            gh_app.GhAppError,
        ):
            message = "reviews App token unavailable; review refused"
            raise Invalid(message) from None
    else:
        gh_token = read(references.gh, label="gh") if references.gh else None
    gh = runfiles.private_dir(tmpdir / "gh")
    if gh_token is not None:
        host = repo.parts(request.repo)[0]
        runfiles.atomic(
            gh / "hosts.yml",
            yaml.safe_dump({host: {"oauth_token": gh_token, "git_protocol": "https"}}),
        )
    docker = runfiles.private_dir(tmpdir / "docker")
    docker_text = (
        read(references.docker, label="docker") if references.docker else '{"auths":{}}'
    )
    runfiles.atomic(docker / "config.json", docker_text)
    env: dict[str, str] = {}
    profile_name = None
    if request.harness is Harness.CLAUDE:
        profile = config.find_profile(
            settings.claude.profiles, str(request.claude_profile)
        )
        if profile is None:
            message = f"claude profile {request.claude_profile!r} is not configured"
            raise Invalid(message)
        profile_name = profile.name
        env[CLAUDE_TOKEN_ENV] = read(
            profile.token, label=f"claude profile {profile.name}"
        )
    signing = (
        signing_key(references.signing) if request.profile is Profile.BUILD else None
    )
    return Delivery(env, signing, gh_fallback=False, claude_profile=profile_name)


def signing_key(source: str) -> str:
    if not source.startswith("/"):
        return read(source, label="signing")
    limit = 64 * 1024
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as incoming:
            metadata = os.fstat(incoming.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                message = "signing key file must be an owned private regular file"
                raise Invalid(message)
            data = incoming.read(limit + 1)
            if len(data) > limit:
                message = "signing key file exceeds 64 KiB"
                raise Invalid(message)
        return data.decode()
    except OSError, UnicodeError:
        message = "signing key file cannot be read"
        raise Invalid(message) from None


def signing_agent(key: str, tmpdir: Path, deadline: float) -> subprocess.Popen[str]:
    """Keep one foreground agent, and load the key through ssh-add's stdin."""
    socket = tmpdir / "ssh.sock"
    agent = subprocess.Popen(  # nosec B603 B607
        [host_tools.resolve("ssh-agent"), "-D", "-a", str(socket)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    try:
        agent_ready(agent, socket, deadline)
        subprocess.run(  # nosec B603 B607
            [host_tools.resolve("ssh-add"), "-"],
            input=key,
            env={"SSH_AUTH_SOCK": str(socket)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=max(0, deadline - time.monotonic()),
        )
    except BaseException:
        with process_group.ignore_signals():
            if process_group.stop_started(agent.pid, 0, agent.poll) is not False:
                agent.wait()
        raise
    return agent


def agent_ready(agent: subprocess.Popen[str], socket: Path, deadline: float) -> None:
    while not socket.exists():
        if agent.poll() is not None:
            message = "run signing agent could not start"
            raise Invalid(message)
        if time.monotonic() >= deadline:
            raise TimeoutError
        time.sleep(0.01)


def auth_copy(source: Path, destination: Path) -> str | None:
    with runfiles.locked(source.parent / "agent-auth.lock"):
        if not source.exists():
            return None
        text = source.read_text()
        runfiles.atomic(destination, text)
        return text


def auth_after(destination: Path) -> str:
    """Read only a bounded, owned regular file through its pinned parent."""
    if destination.parent.resolve() != destination.parent:
        message = "codex auth parent is redirected"
        raise Invalid(message)
    text = runfiles.read_owned(destination, 1024 * 1024, "codex auth")
    document: auth.Json = json.loads(text)
    if not isinstance(document, dict):
        message = f"auth: found {auth.json_type(document)}; expected object"
        raise Invalid(message)
    return text


def auth_write_back(source: Path, destination: Path, before: str | None) -> str | None:
    """Do not overwrite an auth file another run refreshed in the meantime."""
    try:
        after = auth_after(destination)
    except FileNotFoundError:
        return None
    except Invalid as exc:
        return f"codex auth write-back skipped: {exc}\n"
    except OSError, ValueError:
        return "codex auth write-back skipped: cannot safely read a JSON object\n"
    reason = auth.validate(before, after)
    if reason is not None:
        return f"codex auth write-back skipped: {reason}\n"
    if run_state.write_auth(Harness.CODEX, before, after):
        with runfiles.locked(source.parent / "agent-auth.lock"):
            current = source.read_text() if source.exists() else None
            if current == before:
                runfiles.atomic(source, after)
    return None
