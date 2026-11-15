# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha chase-context`: list the silent pull requests of a session's repos.

A Claude Code UserPromptSubmit hook. Claude Code adds its stdout to the model's
context. The user config's `[harness] watchdog_producer` is a script that prints
one row per open pull request, `CHASE name#N sha AGEm ...` when the pull request
has been silent too long. The hook keeps the well-formed CHASE rows younger than
a day, leaves out Renovate's, and prints them under a header, so the model chases
each owner before other work.

The producer reads GitHub, so its rows are cached for ten minutes in one file
that every session of the user shares, and nothing runs while the GitHub budget
(see `alpha gh-shim`) has nothing left. The producer runs with the shim's
directory first on PATH, so its `gh` calls count against that budget. The cache
holds every repository's rows. Each session prints only its own repositories':
`[harness] watchdog_repos` is a list of `"PROJECT=repo,repo"` strings, and the
entry whose PROJECT is the base name of the session's project directory
(`CLAUDE_PROJECT_DIR`, else the `cwd` of the hook's JSON input) picks them. A
session with no matching entry, or no key, prints every row.

The hook only adds context, so any failure prints a note on stderr and exits 0.
Exit 2 would block the user's prompt.
"""

import json
import os
import re
import subprocess  # nosec B404 -- runs the user's own configured script
import sys
import time
from pathlib import Path

from tbhbdev import userconfig
from tbhbdev.alpha import gh_shim

TABLE = "harness"
HEADER = (
    "WATCHDOG: these PRs have had no update for 20+ minutes. "
    "Chase each owner now, before other work:"
)
CACHE_SECONDS = 600
PRODUCER_SECONDS = 40
DAY_MINUTES = 1440
# One well-formed row: CHASE, name#N, sha, then the age in minutes.
ROW = re.compile(r"^CHASE +([a-z-]+)#[0-9]+ +\S+ +([0-9]+)m ")


def cache_path() -> Path:
    """The cache every session of the user shares."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "tbhbdev" / "chase-context.cache"


def keep(line: str) -> bool:
    """Whether LINE is a well-formed CHASE row under a day old, and not Renovate's."""
    found = ROW.match(line)
    if found is None or "renovate" in line.lower():
        return False
    return int(found.group(2)) < DAY_MINUTES


def produce(producer: str) -> list[str]:
    """The rows PRODUCER prints that pass `keep`; none when it fails or hangs."""
    env = {**os.environ, "PATH": gh_shim.prefixed_path(os.environ.get("PATH", ""))}
    try:
        done = subprocess.run(  # nosec B603 -- argv is one configured path
            [str(Path(producer).expanduser())],
            capture_output=True,
            text=True,
            timeout=PRODUCER_SECONDS,
            env=env,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        print("chase-context: the producer failed or timed out", file=sys.stderr)
        return []
    return [line for line in done.stdout.splitlines() if keep(line)]


def rows(producer: str) -> list[str] | None:
    """The cached rows, or a fresh run's; None when the budget is spent."""
    path = cache_path()
    try:
        if time.time() - path.stat().st_mtime < CACHE_SECONDS:
            return path.read_text().splitlines()
    except FileNotFoundError:
        pass
    state = gh_shim.read_state(gh_shim.state_dir() / gh_shim.CORE)
    if state.get("remaining", 1) == 0:
        return None
    found = produce(producer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in found))
    return found


def project_dir() -> str | None:
    """The session's project directory, from the environment or the hook input."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return env
    try:
        cwd = json.load(sys.stdin).get("cwd")
    except ValueError, AttributeError:
        return None
    return cwd if isinstance(cwd, str) else None


def own_repos(entries: list[str] | None, project: str | None) -> set[str] | None:
    """The repositories of the entry whose key best matches PROJECT's base name.

    A key matches a base name equal to it or starting with `key-`. The longest
    key wins and the first entry wins a tie. None means all rows.
    """
    if not entries or not project:
        return None
    name = Path(project).name
    best = -1
    found: set[str] | None = None
    for entry in entries:
        key, _, repos = entry.partition("=")
        key = key.strip()
        if (name == key or name.startswith(f"{key}-")) and len(key) > best:
            best = len(key)
            found = {repo.strip() for repo in repos.split(",")}
    return found


def main() -> int:
    """Print the session's CHASE rows; no config or any failure prints nothing."""
    try:
        producer = userconfig.optional_string(TABLE, "watchdog_producer")
        if producer:
            found = rows(producer)
            own = own_repos(
                userconfig.optional_strings(TABLE, "watchdog_repos"), project_dir()
            )
            shown = [
                line
                for line in found or []
                # A kept row is `CHASE name#N ...`: the word before `#` is the repo.
                if own is None or line.split("#")[0].split()[-1] in own
            ]
            if shown:
                print(HEADER)
                print("\n".join(shown))
    except Exception as exc:  # noqa: BLE001  # a broken hook must not stop a prompt
        print(f"chase-context: {exc}", file=sys.stderr)
    return 0
