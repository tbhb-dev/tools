"""Check that each agent account's signing key is a signing key of tbhb-agent.

A run signs its commits with its account's key. GitHub marks a commit verified
only when that key is registered, and rulesets require verified commits.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from tbhbdev.alpha.agent import credentials, host_tools, transport
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.decisions import remote
from tbhbdev.alpha.agent.model import Host, Invalid, Settings

KEYS_URL = "https://api.github.com/users/tbhb-agent/ssh_signing_keys"


def fingerprint(public_key: str) -> str:
    """The `SHA256:` fingerprint of an OpenSSH public key line."""
    try:
        _, encoded, *_ = public_key.split()
    except ValueError:
        message = "not an OpenSSH public key"
        raise ValueError(message) from None
    blob = base64.b64decode(encoded, validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def host_fingerprint(settings: Settings, account: str) -> str:
    """The fingerprint of the public half of `account`'s signing key."""
    references = next((c for c in settings.credentials if c.account == account), None)
    if references is None:
        message = f"account {account!r} has no signing key"
        raise Invalid(message)
    key = credentials.signing_key(references.signing)
    # Load the key the way a run does, so the agent shows what a run signs with.
    with tempfile.TemporaryDirectory(prefix="sp") as directory:
        agent = credentials.signing_agent(key, Path(directory), time.monotonic() + 15)
        try:
            listed = subprocess.run(  # nosec B603
                [host_tools.resolve("ssh-add"), "-L"],
                env={"SSH_AUTH_SOCK": str(Path(directory) / "ssh.sock")},
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
        finally:
            agent.terminate()
            agent.wait()
    return fingerprint(listed.stdout)


def host_main(argv: list[str], prog: str) -> int:
    """Print the fingerprint, and nothing else, of the account's signing key."""
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("account")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        print(host_fingerprint(agent_settings.load(), args.account))
    except (Invalid, OSError, ValueError, subprocess.SubprocessError) as exc:
        # The message names no key text: errors above carry paths and labels only.
        print(f"{prog}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def registered() -> set[str]:
    """The fingerprints of tbhb-agent's public signing keys."""
    request = urllib.request.Request(
        KEYS_URL, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310  # nosec B310
        entries = json.load(response)
    return {fingerprint(entry["key"]) for entry in entries}


def remote_fingerprint(settings: Settings, host: Host) -> str:
    script = (
        remote.PREFIX + f"{remote.program(host)} alpha agent signing-fingerprint "
        f"{host.account}\n"
    )
    return transport.shell(settings, host, script).strip()


def check(settings: Settings, known: set[str]) -> tuple[list[str], list[str]]:
    """The host lines, and the names of hosts that are not covered."""
    lines: list[str] = []
    missing: list[str] = []
    for host in settings.hosts:
        try:
            found = remote_fingerprint(settings, host)
            covered = found in known
            lines.append(f"{host.name}: {found} {'listed' if covered else 'MISSING'}")
        except (transport.TransportError, transport.CollisionError) as exc:
            covered = False
            lines.append(f"{host.name}: unreadable ({exc})")
        if not covered:
            missing.append(host.name)
    return lines, missing


def main(settings: Settings) -> int:
    try:
        known = registered()
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        print(
            f"signing-parity: cannot read the signing key list: {exc}", file=sys.stderr
        )
        return 1
    lines, missing = check(settings, known)
    print("\n".join(lines))
    if missing:
        print(f"not covered: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0
