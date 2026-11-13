"""Best-effort identities of the host checkout and installed distribution."""

import re
import subprocess  # nosec B404
from importlib import metadata
from pathlib import Path

from tbhbdev import version


def wheel_commit() -> str:
    try:
        dist = metadata.distribution(version.NAME)
        commit = version.identity(dist.read_text("direct_url.json"))
    except metadata.PackageNotFoundError, OSError, ValueError:
        return version.UNKNOWN
    return (
        commit
        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit)
        else version.UNKNOWN
    )


def tools_commit() -> str:
    try:
        checkout = Path(__file__).resolve().parents[4]
        if not (checkout / ".git").exists():
            return version.UNKNOWN
        return subprocess.check_output(  # nosec B603 B607
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except OSError, subprocess.SubprocessError:
        return version.UNKNOWN


def identities() -> dict[str, str]:
    return {"tools_commit": tools_commit(), "wheel_commit": wheel_commit()}


def line() -> str:
    return "tbhbdev " + " ".join(
        f"{key}={value}" for key, value in identities().items()
    )
