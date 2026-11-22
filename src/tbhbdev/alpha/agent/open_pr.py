"""Open the pull request for a run's pushed branch, host-side, with the run's token.

The pushed commit already passed the push scan, and the draft is part of it.
"""

from __future__ import annotations

import subprocess  # nosec B404
from typing import TYPE_CHECKING, cast

import httpx

from tbhbdev.alpha import pull_request
from tbhbdev.alpha.agent.decisions import repo

if TYPE_CHECKING:
    from pathlib import Path

DRAFT = ".github/PULL_REQUEST_DRAFT.md"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class OpenRefusedError(ValueError):
    """The draft or the API refused; the message is safe to log."""


def read_draft(workspace: Path, commit: str) -> pull_request.Checked:
    """The draft as the pushed commit holds it, checked."""
    done = subprocess.run(  # nosec B603
        ["git", "show", f"{commit}:{DRAFT}"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        message = f"the pushed commit has no {DRAFT}"
        raise OpenRefusedError(message)
    checked = pull_request.check_text(done.stdout)
    if checked.problems:
        line, why = checked.problems[0]
        message = f"the draft fails its check at line {line}: {why}"
        raise OpenRefusedError(message)
    return checked


def open_pull_request(
    workspace: Path, commit: str, repository: str, branch: str, token: str
) -> int:
    """Create the PR and return its number.

    Raises:
        OpenRefusedError: The draft is missing or fails its check, or GitHub
            refuses or cannot be reached.
    """
    checked = read_draft(workspace, commit)
    body = pull_request.body_of(checked.rendered)
    _, owner, name = repo.parts(repository)
    try:
        response = httpx.post(
            f"https://api.github.com/repos/{owner}/{name}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "title": checked.title,
                "body": body,
                "head": branch,
                "base": pull_request.base_of(checked.meta),
                "draft": bool(checked.meta.get("draft")),
            },
            timeout=TIMEOUT,
        )
        number = cast("dict[str, int]", response.json()).get("number")
    except (httpx.HTTPError, ValueError) as exc:
        message = f"the PR request failed: {type(exc).__name__}"
        raise OpenRefusedError(message) from exc
    if response.status_code != httpx.codes.CREATED or not isinstance(number, int):
        message = f"GitHub refused the PR with status {response.status_code}"
        raise OpenRefusedError(message)
    return number
