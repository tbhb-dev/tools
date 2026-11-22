"""Reading the launching session's name and id from the harness."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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
        result = subprocess.run(
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


def load_table() -> tuple[dict[str, frozenset[str] | None], dict[str, str]]:
    """The built-in launchers plus the user's table, and the owner exceptions.

    The table is a JSON object with optional ``launchers`` (session name to a
    list of ``org/name`` repositories, or null for any) and ``owners``
    (repository name to its owner session). A missing file adds nothing.
    """
    launchers = dict(rules.REVIEW_LAUNCHERS)
    try:
        table = json.loads(table_path().read_text())
        for name, repos in table.get("launchers", {}).items():
            launchers[str(name)] = None if repos is None else frozenset(map(str, repos))
        owners = {str(k): str(v) for k, v in table.get("owners", {}).items()}
    except FileNotFoundError:
        return launchers, {}
    except (ValueError, AttributeError, TypeError, OSError) as exc:
        message = f"{table_path()}: invalid review-launcher table"
        raise Invalid(message) from exc
    return launchers, owners
