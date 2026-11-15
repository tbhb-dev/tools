"""`tbhbdev --version`: the installed version and which build it is.

The package's own version is static, so it can't tell two builds apart. The
build is named by PEP 610's `direct_url.json` in the installed dist-info: the
commit for an install from a git URL, or the directory for an editable or
local install. Reading it takes no subprocess and no network.

A build can also carry a stamp: `tbhbdev/_build.py` with `COMMIT`, the
40-character sha. Nothing writes it yet, and it is never committed. It names
the build only when `direct_url.json` doesn't (a wheel install has no
`vcs_info`), so a stray copy in a checkout can't mask what an install records.
"""

from __future__ import annotations

import json
import re
from importlib import import_module, metadata
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from types import ModuleType

NAME = "tbhbdev"
UNKNOWN = "unknown"
BUILD_MODULE = "tbhbdev._build"
COMMIT = re.compile(r"[0-9a-f]{40}")


def build_module() -> ModuleType | None:
    """The release stamp module, or None in a tree or install without one."""
    try:
        return import_module(BUILD_MODULE)
    except Exception:  # noqa: BLE001  # a bad stamp must never break --version
        return None


def stamped(build: ModuleType | None) -> str | None:
    """The 40-hex `COMMIT` that BUILD holds, or None when absent or malformed."""
    commit = getattr(build, "COMMIT", None)
    if isinstance(commit, str) and COMMIT.fullmatch(commit):
        return commit
    return None


def identity(text: str | None, build: ModuleType | None = None) -> str:
    """Name the build: what `direct_url.json` TEXT records, else BUILD's stamp."""
    recorded = direct_url_identity(text)
    return recorded if recorded != UNKNOWN else stamped(build) or UNKNOWN


def direct_url_identity(text: str | None) -> str:
    """Name the build that `direct_url.json` TEXT records, or `unknown`."""
    if text is None:
        return UNKNOWN
    try:
        record = json.loads(text)
    except ValueError:
        return UNKNOWN
    if not isinstance(record, dict):
        return UNKNOWN
    vcs = record.get("vcs_info")
    if isinstance(vcs, dict):
        commit = vcs.get("commit_id")
        if isinstance(commit, str) and commit:
            return commit
    directory = record.get("dir_info")
    url = record.get("url")
    if isinstance(directory, dict) and isinstance(url, str):
        path = unquote(urlparse(url).path)
        if path:
            kind = "editable" if directory.get("editable") is True else "local"
            return f"{kind} {path}"
    return UNKNOWN


def line() -> str:
    """The `--version` line for the installed distribution."""
    dist = metadata.distribution(NAME)
    recorded = direct_url_identity(dist.read_text("direct_url.json"))
    build = recorded if recorded != UNKNOWN else identity(None, build_module())
    return f"{NAME} {dist.version} ({build})"
