"""Reading the launching session's name and id from the harness."""

from __future__ import annotations

import os
import subprocess  # nosec B404

from tbhbdev.alpha.agent.decisions import launcher as rules

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
