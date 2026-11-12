"""In-memory repository credentials for the ops deployer.

All inputs are explicit. JWT clock refusals are reported as ``mint``: gh_app
sanitizes API failures before returning them, so they cannot be distinguished.
The module entry point is Git's private credential helper, not an alpha command.
Git runs with hooks disabled so hooks cannot read the credential pipe.
Git always receives ``GIT_TERMINAL_PROMPT=0`` to prevent interactive prompts.
Connect endpoints must be HTTPS URLs with a host.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from tbhbdev import secrets
from tbhbdev.alpha import gh_app
lazy from tbhbdev.alpha.gh_app import Minted

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# cspell:words extraheader

type Kind = Literal[
    "token_file", "connect", "item", "installation", "clock", "mint", "git"
]
MESSAGES: dict[Kind, str] = {
    "token_file": "Credential file is unavailable or unsafe",  # nosec B105
    "connect": "Cannot reach the supplied Connect endpoints",
    "item": "The App item or its required fields are unavailable",
    "installation": "The repository has no App installation",
    "clock": "The App JWT was refused because of the clock",
    "mint": "Cannot mint the repository token",
    "git": "Cannot run Git with the repository token",
}
# Both platforms start on the pipe path; real Git probes verify inheritance.
PIPE_BY_PLATFORM = {"linux": True, "darwin": True}
USE_PIPE = PIPE_BY_PLATFORM.get(sys.platform, True)
NO_CHECK = False
PRIVATE_MODE = 0o600
TWO_PARTS = 2
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# mint_token uses only the clock and transport; its other Env slots are inert.
_MINT_ENV = gh_app.Env(Path(), gh_app.clock, secrets.Store, gh_app.run_openssl)


class RepoTokenError(Exception):
    """A fixed, credential-free message and a machine-readable failure kind."""

    def __init__(self, *, kind: Kind) -> None:
        self.kind = kind
        super().__init__(MESSAGES[kind])


@dataclass(frozen=True)
class AppKey:
    app_id: str = field(repr=False)
    private_key: str = field(repr=False)


def _read(path: Path) -> str:
    """Read only after checking the opened parent and file descriptors."""
    directory = os.open(path.parent, DIRECTORY_FLAGS)
    try:
        parent = os.fstat(directory)
        if parent.st_uid not in {0, os.geteuid()} or parent.st_mode & 0o022:
            raise RepoTokenError(kind="token_file")
        descriptor = os.open(
            path.name,
            FILE_FLAGS,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as stream:
            found = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(found.st_mode)
                or stat.S_IMODE(found.st_mode) != PRIVATE_MODE
                or found.st_uid != os.geteuid()
            ):
                raise RepoTokenError(kind="token_file")
            data = stream.read()
    finally:
        os.close(directory)
    return data.decode()


def read_app(token_file: Path, item: str, *, endpoints: Sequence[str]) -> AppKey:
    """Read an App key using only the supplied token file and endpoints."""
    try:
        token = _read(token_file).strip()
        if not token:
            raise RepoTokenError(kind="token_file")
    except OSError, UnicodeError:
        raise RepoTokenError(kind="token_file") from None
    parts = item.removeprefix("op://").split("/")
    if not item.startswith("op://") or len(parts) != TWO_PARTS or not all(parts):
        raise RepoTokenError(kind="item")
    try:
        for endpoint in endpoints:
            url = httpx.URL(endpoint)
            if url.scheme != "https" or not url.host:
                raise RepoTokenError(kind="connect")
    except httpx.InvalidURL, ValueError, TypeError:
        raise RepoTokenError(kind="connect") from None
    try:
        found = secrets.explicit_item(token, *parts, urls=endpoints)
    except secrets.ConnectUnavailableError:
        raise RepoTokenError(kind="connect") from None
    except secrets.SecretsError:
        raise RepoTokenError(kind="item") from None
    values = {entry.label: entry.value for entry in found.fields}
    if (
        not values.get("app_id", "").strip()
        or not values.get("private_key", "").strip()
    ):
        raise RepoTokenError(kind="item")
    return AppKey(values["app_id"].strip(), values["private_key"])


def _cache(path: Path) -> dict[str, int]:
    try:
        data = _read(path)
    except FileNotFoundError:
        return {}
    except OSError, UnicodeError:
        raise RepoTokenError(kind="token_file") from None
    try:
        value: object = json.loads(data)
        if not isinstance(value, dict):
            raise RepoTokenError(kind="token_file")
        entries: dict[str, object] = value
        if any(type(ident) is not int or ident <= 0 for ident in entries.values()):
            raise RepoTokenError(kind="token_file")
    except ValueError:
        raise RepoTokenError(kind="token_file") from None
    return value


def _write_cache(path: Path, cache: dict[str, int]) -> None:
    try:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(cache, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)  # noqa: PTH105  # Explicit atomic replace.
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError:
        raise RepoTokenError(kind="token_file") from None


def _mint(
    app: AppKey, repository: str, permissions: Mapping[str, str], cache: dict[str, int]
) -> Minted:
    try:
        return gh_app.mint_token(
            _MINT_ENV,
            app_id=app.app_id,
            pem=app.private_key,
            repository=repository,
            permissions=permissions,
            known_installation=cache.get(repository),
            installation_found=lambda ident: cache.update({repository: ident}),
        )
    except gh_app.InstallationMissingError:
        raise RepoTokenError(kind="installation") from None
    except gh_app.CachedInstallationMissingError:
        raise
    except gh_app.GhAppError:
        raise RepoTokenError(kind="mint") from None


def mint(
    app: AppKey,
    repository: str,
    permissions: Mapping[str, str],
    *,
    installation_cache: Path | None = None,
) -> Minted:
    """Mint a token scoped to one repository and the requested permissions."""
    if installation_cache is None:
        return _mint(app, repository, permissions, {})
    cache = _cache(installation_cache)
    try:
        result = _mint(app, repository, permissions, cache)
    except gh_app.CachedInstallationMissingError:
        del cache[repository]
        _write_cache(installation_cache, cache)
        result = _mint(app, repository, permissions, cache)
    _write_cache(installation_cache, cache)
    return result


def _extraheader(env: Mapping[str, str], token: str) -> dict[str, str]:
    result = dict(env)
    count = int(result.get("GIT_CONFIG_COUNT", "0"))
    if count < 0:
        raise ValueError
    value = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    result["GIT_CONFIG_COUNT"] = str(count + 1)
    result[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraheader"
    result[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {value}"
    return result


def _git(  # noqa: PLR0913  # Private input seam for the real Git probe.
    token: Minted,
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    descriptor = None
    try:
        path = env.get("PATH")
        if path is None:
            raise RepoTokenError(kind="git")
        executable = shutil.which("git", path=path)
        if executable is None:
            raise RepoTokenError(kind="git")
        child_env = dict(env)
        helper = ""
        descriptors: tuple[int, ...] = ()
        if USE_PIPE:
            descriptor, writer = os.pipe()
            try:
                os.write(writer, token.token.encode())
            finally:
                os.close(writer)
            descriptors = (descriptor,)
            helper = "!" + shlex.join(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "tbhbdev.alpha.repo_token",
                    str(descriptor),
                ]
            )
        else:
            child_env = _extraheader(env, token.token)
        child_env["GIT_TERMINAL_PROMPT"] = "0"
        return subprocess.run(  # nosec B603
            [
                executable,
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper={helper}",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=cwd,
            env=child_env,
            timeout=timeout,
            capture_output=True,
            text=True,
            pass_fds=descriptors,
            input=input_text,
            check=NO_CHECK,
        )
    except OSError, subprocess.TimeoutExpired, ValueError:
        raise RepoTokenError(kind="git") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def git(
    token: Minted,
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    """Run Git with an in-memory token, disabled hooks and no terminal prompts."""
    return _git(token, args, cwd=cwd, env=env, timeout=timeout)


def _helper(argv: Sequence[str]) -> None:
    if len(argv) != TWO_PARTS or argv[1] != "get":
        return
    request = {
        key: value
        for key, _, value in (
            line.rstrip("\n").partition("=") for line in sys.stdin if line.strip()
        )
    }
    if request.get("protocol") != "https" or request.get("host") != "github.com":
        return
    with os.fdopen(int(argv[0])) as stream:
        value = stream.read()
    sys.stdout.write(f"username=x-access-token\npassword={value}\n\n")


if __name__ == "__main__":
    _helper(sys.argv[1:])
