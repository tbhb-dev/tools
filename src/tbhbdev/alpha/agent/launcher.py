"""Reading the launching session's name and id from the harness."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
from pathlib import Path
from typing import cast

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions import launcher as rules
from tbhbdev.alpha.agent.model import Invalid

TMUX_TIMEOUT_SECONDS = 5


def session() -> tuple[str | None, str | None]:
    """The launching session's name from its tmux pane title, and its session id."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None, session_id
    try:
        result = subprocess.run(  # nosec B603 B607 -- fixed tmux argv
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_title}"],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_SECONDS,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return None, session_id
    return rules.session_name(result.stdout), session_id


def table_path() -> Path:
    return runfiles.state_home(Path.home()) / "tbhbdev/agent-review-launchers.json"


Table = tuple[dict[str, frozenset[str] | None], dict[str, str]]


def _parse(table: object) -> Table | None:
    """The launchers and owners of a decoded table, or None when it is malformed.

    Values are never coerced: a string where a list belongs, or a null owner,
    is malformed.
    """
    if not isinstance(table, dict):
        return None
    parts = cast("dict[str, object]", table)
    listed = parts.get("launchers", {})
    owned = parts.get("owners", {})
    if not isinstance(listed, dict) or not isinstance(owned, dict):
        return None
    launchers = dict(rules.REVIEW_LAUNCHERS)
    for name, repos in cast("dict[str, object]", listed).items():
        if repos is None:
            launchers[name] = None
        elif isinstance(repos, list) and all(
            isinstance(repo, str) for repo in cast("list[object]", repos)
        ):
            launchers[name] = frozenset(cast("list[str]", repos))
        else:
            return None
    owners = cast("dict[str, object]", owned)
    if not all(isinstance(owner, str) for owner in owners.values()):
        return None
    return launchers, cast("dict[str, str]", owners)


def load_table() -> Table:
    """The built-in launchers plus the user's table, and the owner exceptions.

    The table is a JSON object with optional ``launchers`` (session name to a
    list of ``org/name`` repositories, or null for any) and ``owners``
    (repository name to its owner session). A missing, unreadable or malformed
    file refuses the launch, naming the path.
    """
    message = f"{table_path()}: invalid review-launcher table"
    try:
        parsed = _parse(json.loads(table_path().read_text()))
    except (ValueError, OSError) as exc:
        raise Invalid(message) from exc
    if parsed is None:
        raise Invalid(message)
    return parsed
