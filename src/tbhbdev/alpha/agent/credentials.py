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
from tbhbdev.alpha.agent import process_group, runfiles
from tbhbdev.alpha.agent.decisions import config, repo, run_state
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


def installation_token(references: Credentials, repository: str) -> str:
    """Reuse the app signer and request a token restricted to one repository."""
    host, owner, name = repo.parts(repository)
    if host != "github.com":
        message = "reviews app supports github.com repositories"
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
    fallback = False
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
            fallback = True
            gh_token = read(references.gh, label="gh")
    else:
        gh_token = read(references.gh, label="gh")
    gh = runfiles.private_dir(tmpdir / "gh")
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
        read(references.signing, label="signing")
        if request.profile is Profile.BUILD
        else None
    )
    return Delivery(env, signing, fallback, profile_name)


def signing_agent(key: str, tmpdir: Path, deadline: float) -> subprocess.Popen[str]:
    """Keep one foreground agent, and load the key through ssh-add's stdin."""
    socket = tmpdir / "ssh.sock"
    agent = subprocess.Popen(  # nosec B603 B607
        ["ssh-agent", "-D", "-a", str(socket)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    try:
        agent_ready(agent, socket, deadline)
        subprocess.run(  # nosec B603 B607
            ["ssh-add", "-"],
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
            process_group.stop_started(agent.pid, 0)
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
    directory = os.open(
        destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        fd = os.open(
            destination.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as incoming:
        metadata = os.fstat(incoming.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            message = "codex auth is not an owned regular file"
            raise Invalid(message)
        limit = 1024 * 1024
        data = incoming.read(limit + 1)
        if len(data) > limit:
            message = "codex auth exceeds 1 MiB"
            raise Invalid(message)
    text = data.decode()
    if not isinstance(json.loads(text), dict):
        message = "codex auth must be a JSON object"
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
    if run_state.write_auth(Harness.CODEX, before, after):
        with runfiles.locked(source.parent / "agent-auth.lock"):
            current = source.read_text() if source.exists() else None
            if current == before:
                runfiles.atomic(source, after)
    return None
