"""`tbhbdev --version`: the installed version and which build it is.

The package's own version is static, so it can't tell two builds apart. The
build is named by PEP 610's `direct_url.json` in the installed dist-info: the
commit for an install from a git URL, or the directory for an editable or
local install. Reading it takes no subprocess and no network.
"""

import json
from importlib import metadata
from urllib.parse import unquote, urlparse

NAME = "tbhbdev"
UNKNOWN = "unknown"


def identity(text: str | None) -> str:
    """Name the build that `direct_url.json` text records, or `unknown`."""
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
    return f"{NAME} {dist.version} ({identity(dist.read_text('direct_url.json'))})"
