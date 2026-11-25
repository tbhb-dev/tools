"""Synchronize a version mismatch once before the driver launches a run."""

from __future__ import annotations

import os
import shlex
import subprocess  # nosec B404
import sys
from typing import TYPE_CHECKING

from tbhbdev import version
from tbhbdev.alpha import version_check
from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import driver as rules
from tbhbdev.alpha.agent.decisions import remote
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from typing import NoReturn

    from tbhbdev.alpha.agent.model import Host, Settings

_RETRY = "_TBHB_AGENT_VERSION_HEAL"
_INSTALL_TIMEOUT = 300
_INSTALL = ("uv", "tool", "install", "--no-config", "--force")


def read_origin(argv: list[str]) -> tuple[int, str]:
    flags = ("-c", "credential.helper=", "-c", "http.extraHeader=")
    try:
        result = subprocess.run(  # nosec B603
            [argv[0], *flags, *argv[1:]],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            errors="replace",
            timeout=version_check.TIMEOUT,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return 1, ""
    return result.returncode, result.stdout


def restart(mismatch: str) -> NoReturn:
    os.environ[_RETRY] = mismatch
    try:
        # Open the entry point again so its shebang selects the new environment.
        if sys.argv[0].endswith("/tbhbdev/__main__.py"):
            os.execv(sys.executable, sys.orig_argv)  # noqa: S606  # nosec B606
        os.execv(sys.argv[0], sys.argv)  # noqa: S606  # nosec B606
    finally:
        os.environ.pop(_RETRY, None)


def check_retry(local: str, installed: str, original: str) -> None:
    try:
        rules.same_version(local, installed)
    except Invalid as exc:
        message = f"{original} (after one sync)"
        raise Invalid(message) from exc


def check(settings: Settings, host: Host, local: str, installed: str) -> str | None:
    """Check parity and return the original error if the host needs a reread."""
    previous = os.environ.pop(_RETRY, None)
    if previous is not None:
        check_retry(local, installed, previous)
        return None
    try:
        rules.same_version(local, installed)
    except Invalid as mismatch:
        head = version_check.latest(read_origin)
        if head is None:
            raise
        host_commit = version_check.parse_stamp(installed) or version.UNKNOWN
        driver_commit = version_check.parse_stamp(local) or version.UNKNOWN
        if host_commit == driver_commit == head:
            raise
        install = (*_INSTALL, f"git+{version_check.REPO_URL}@{head}")
        try:
            if host_commit != head:
                transport.shell(
                    settings,
                    host,
                    remote.PREFIX + "exec " + shlex.join(install),
                    timeout=_INSTALL_TIMEOUT,
                )
                print(
                    f"version heal: host {host.name} {host_commit}->{head}",
                    file=sys.stderr,
                )
            if driver_commit != head:
                subprocess.run(  # nosec B603 B607
                    install,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_INSTALL_TIMEOUT,
                    check=True,
                )
                print(
                    f"version heal: driver {driver_commit}->{head}",
                    file=sys.stderr,
                    flush=True,
                )
                restart(str(mismatch))
        except (OSError, subprocess.SubprocessError) as exc:
            message = f"{mismatch} (version sync failed: {exc})"
            raise Invalid(message) from None
        return str(mismatch)
    return None
