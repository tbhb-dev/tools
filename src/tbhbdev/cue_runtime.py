"""Fetch, verify and locate the pinned cue binary.

The content kernel runs cue only from the path `resolve` returns. There is no
PATH lookup and no environment override, so a missing or wrong binary stops
the check ("run tbhbdev cue install") instead of falling back to another cue.
The version and digests live in `cue-pin.json`, a copy of rules'
`pins/tools.json`.
"""

import hashlib
import io
import json
import os
import platform
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

type Pin = dict[str, object]

RELEASES = "https://github.com/cue-lang/cue/releases/download/"
PLATFORMS = {
    ("Darwin", "arm64"): "darwin_arm64",
    ("Linux", "x86_64"): "linux_amd64",
    ("Linux", "amd64"): "linux_amd64",
    ("Linux", "aarch64"): "linux_arm64",
    ("Linux", "arm64"): "linux_arm64",
}
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class CueError(RuntimeError):
    """The pinned cue is missing, wrong, or could not be installed."""


def load_pin() -> Pin:
    text = files("tbhbdev").joinpath("cue-pin.json").read_text(encoding="utf-8")
    return cast("Pin", json.loads(text)["cue"])


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    pair = (system or platform.system(), machine or platform.machine())
    key = PLATFORMS.get(pair)
    if key is None:
        message = f"no pinned cue for {pair[0]} {pair[1]}"
        raise CueError(message)
    return key


def binary_path(pin: Pin | None = None) -> Path:
    version = str((pin or load_pin())["version"])
    home = Path.home()
    configured = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    data = configured if configured.is_absolute() else home / ".local/share"
    return data / "tbhbdev" / "cue" / version / "cue"


def resolve(pin: Pin | None = None) -> Path:
    """Return the pinned cue binary, or refuse."""
    pin = pin or load_pin()
    path = binary_path(pin)
    if not path.is_file() or not os.access(path, os.X_OK):
        message = (
            f"cue {pin['version']} is not installed at {path}; run tbhbdev cue install"
        )
        raise CueError(message)
    # Fixed argument list; the path is the pinned install location.
    done = subprocess.run(  # nosec B603
        [str(path), "version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    lines = done.stdout.splitlines()
    found = lines[0].split()[-1] if lines and lines[0].split() else ""
    if found != f"v{pin['version']}":
        message = (
            f"{path} reports {found or 'no version'}, not v{pin['version']}; "
            "run tbhbdev cue install"
        )
        raise CueError(message)
    return path


def _fetch(url: str) -> bytes:
    response = httpx.get(url, follow_redirects=True, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content


def install(
    pin: Pin | None = None,
    *,
    key: str | None = None,
    fetch: Callable[[str], bytes] = _fetch,
) -> Path:
    """Install the pinned cue unless it is already there and current."""
    pin = pin or load_pin()
    try:
        return resolve(pin)
    except CueError:
        pass
    platforms = cast("dict[str, dict[str, str]]", pin["platforms"])
    entry = platforms.get(key or platform_key())
    if entry is None or not entry["url"].endswith(".tar.gz"):
        message = "no pinned cue archive for this platform"
        raise CueError(message)
    if not entry["url"].startswith(RELEASES):
        message = f"refusing a cue download outside {RELEASES}"
        raise CueError(message)
    data = fetch(entry["url"])
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry["sha256"]:
        message = (
            f"cue download digest {digest} does not match the pin {entry['sha256']}"
        )
        raise CueError(message)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = next((m for m in tar if m.name == "cue" and m.isfile()), None)
        extracted = tar.extractfile(member) if member else None
        if extracted is None:
            message = "the cue archive has no cue binary"
            raise CueError(message)
        binary = extracted.read()
    target = binary_path(pin)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=target.parent)
    with os.fdopen(handle, "wb") as out:
        out.write(binary)
    Path(name).chmod(0o755)
    Path(name).replace(target)
    return resolve(pin)


def main(argv: Sequence[str], prog: str) -> int:
    """`tbhbdev cue install` or `tbhbdev cue path`."""
    if list(argv) not in (["install"], ["path"]):
        sys.stderr.write(f"usage: {prog} install | path\n")
        return 2
    try:
        path = install() if argv[0] == "install" else resolve()
    except (CueError, httpx.HTTPError, OSError) as exc:
        sys.stderr.write(f"{prog}: {exc}\n")
        return 1
    sys.stdout.write(f"{path}\n")
    return 0
