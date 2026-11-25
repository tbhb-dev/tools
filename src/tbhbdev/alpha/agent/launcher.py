"""Reading the launching session's name and id from the harness."""

from __future__ import annotations

import os
import subprocess  # nosec B404

from tbhbdev.alpha import worker_identity
from tbhbdev.alpha.agent.decisions import launcher as rules
from tbhbdev.alpha.agent.model import Invalid

TMUX_TIMEOUT_SECONDS = 5


def session(
    workflow: str | None = None, registered: str | None = None
) -> tuple[str | None, str | None]:
    """The launching session's name from its tmux pane title, and its session id.

    With no tmux pane, WORKFLOW names the registered worker's launch as
    `worker/<WORKFLOW>`. REGISTERED is the `[agent.worker] path`.

    Raises:
        Invalid: WORKFLOW is given with no pane and the caller isn't a
            descendant of the registered worker.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        if workflow is None:
            return None, session_id
        if why := worker_identity.refusal(registered):
            message = f"--worker-workflow needs the registered worker: {why}"
            raise Invalid(message)
        return worker_identity.name(workflow), session_id
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
