"""Read the launching session and resolve a pull request to its build branch."""

from __future__ import annotations

import os
import subprocess  # nosec B404
from typing import cast

import httpx

from tbhbdev.alpha.agent.decisions import launcher as rules
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.alpha.gh_app import API

TMUX_TIMEOUT_SECONDS = 5
PULL_TIMEOUT_SECONDS = 10


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


def pull_branch(repository: str, number: str) -> str:
    """Read the head branch of a single pull request in the same repository."""
    host, owner, name = repo.parts(repository)
    slug = f"{owner}/{name}"
    if host.casefold() != repo.DEFAULT_GIT_HOST:
        message = "pull request head lookup requires the default git host"
        raise Invalid(message)
    try:
        response = httpx.get(
            f"{API}/repos/{slug}/pulls/{number}", timeout=PULL_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        head = cast("dict[str, object]", response.json()["head"])
        branch = head["ref"]
        source = cast("dict[str, object]", head["repo"])["full_name"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        message = f"cannot read the head branch of {slug}#{number}"
        raise Invalid(message) from exc
    if not isinstance(source, str) or source.casefold() != slug.casefold():
        message = f"{slug}#{number} must have its head in the same repository"
        raise Invalid(message)
    if not isinstance(branch, str):
        message = f"cannot read the head branch of {slug}#{number}"
        raise Invalid(message)
    return branch


def review_build_name(repository: str, ref: str) -> str:
    if pull := rules.PULL_HEAD.fullmatch(ref):
        ref = f"refs/heads/{pull_branch(repository, pull[1])}"
    return rules.build_name(ref)
