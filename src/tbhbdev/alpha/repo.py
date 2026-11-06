"""Where an alpha hook runs: its repository, worktree and origin.

Every harness hook that `tbhbdev alpha install` writes is user-level, so it
fires in every repository the harness works in. Each one first asks
`in_tbhb_dev` whether the repository it runs for has a GitHub `tbhb-dev`
origin, and exits 0 with no output when it doesn't.

The repository comes from the hook payload, never from `$CLAUDE_PROJECT_DIR`
or a path relative to the hook: Claude Code and Codex send `cwd`, and
Antigravity, which runs user-level hooks in its own config directory, sends
`workspacePaths`. Without either, the process's own directory stands in.
"""

import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import cast

type Json = dict[str, object]

# git@github.com:tbhb-dev/x, ssh://git@github.com/tbhb-dev/x(.git) and
# https://github.com/tbhb-dev/x, with an optional user, port and trailing slash,
# and whitespace around it.
#
# One POSIX ERE serves both checks, so they can't drift. The identity git hook
# shims, which have to work without tbhbdev, grep a whole origin with it under
# LC_ALL=C. Python matches a whole origin against it, with [:space:] spelled \s,
# under re.ASCII so the classes and the case folding are the C locale's.
TBHB_DEV_ORIGIN_ERE = (
    r"[[:space:]]*"
    r"((https|ssh|git\+ssh)://([^@/[:space:]]+@)?(ssh\.)?github\.com(:[0-9]+)?/"
    r"|[^@/:[:space:]]+@github\.com:)tbhb-dev/[^/[:space:]]+/?"
    r"[[:space:]]*"
)
TBHB_DEV_ORIGIN = re.compile(
    TBHB_DEV_ORIGIN_ERE.replace("[:space:]", r"\s"), re.IGNORECASE | re.ASCII
)


def git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run git; return its exit code and stdout without trailing newlines.

    A `cwd` that no longer exists is exit 128, git's own status for a
    directory it can't use.
    """
    try:
        result = subprocess.run(  # nosec B603 B607
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
    except OSError:
        return 128, ""
    return result.returncode, result.stdout.strip("\n")


def is_tbhb_dev_url(url: str) -> bool:
    """Whether a remote URL names a repository of the GitHub tbhb-dev org."""
    return TBHB_DEV_ORIGIN.fullmatch(url) is not None


def in_tbhb_dev(cwd: Path | None = None) -> bool:
    """Whether `cwd` is in a repository whose origin is a tbhb-dev repository."""
    code, url = git("remote", "get-url", "origin", cwd=cwd)
    return code == 0 and is_tbhb_dev_url(url)


def hook_cwd(data: Json) -> Path | None:
    """The directory a hook payload works in: `cwd`, else the first workspace.

    None when the payload names neither, so the caller uses its own.
    """
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    workspaces = data.get("workspacePaths")
    if isinstance(workspaces, list):
        # cast is a runtime no-op, so no test could tell a mutated type string.
        listed = cast("list[object]", workspaces)  # pragma: no mutate
        first = next(iter(listed), None)
        if isinstance(first, str) and first:
            return Path(first)
    return None


def read_hook_stdin() -> Json:
    """Hook JSON from stdin; an empty dict if empty, invalid or not an object."""
    try:
        data = json.loads(sys.stdin.read())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
