"""Open the pull request for a run's pushed branch, host-side, with the run's token.

The pushed commit already passed the push scan, and the draft is part of it.
"""

from __future__ import annotations

import subprocess  # nosec B404
from typing import TYPE_CHECKING, NamedTuple, cast

import httpx

from tbhbdev.alpha import pull_request
from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.decisions import repo

if TYPE_CHECKING:
    from pathlib import Path

DRAFT = ".github/PULL_REQUEST_DRAFT.md"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class OpenRefusedError(ValueError):
    """The draft or the API refused; the message is safe to log."""


class Draft(NamedTuple):
    """The parts of a draft that the API call needs."""

    title: str
    body: str
    base: str
    draft: bool


def parse_draft(text: str) -> Draft:
    """Split a draft into its title, body and frontmatter fields.

    Only the frontmatter parser runs here. The schema, the section and prose
    checks and the Conventional Commit check read the repository or need tools
    that run hosts lack, so the run's own ``pr check`` and the push scan stand
    for them.

    Raises:
        OpenRefusedError: The frontmatter is invalid or names no base, or the
            body doesn't start with an H1 title.
    """
    try:
        meta, body = pull_request.parse_frontmatter(text)
    except pull_request.FrontmatterError as exc:
        message = f"the draft frontmatter is invalid: {exc}"
        raise OpenRefusedError(message) from exc
    fields = cast("dict[str, object]", meta) if isinstance(meta, dict) else {}
    base = fields.get("base")
    if not isinstance(base, str) or not base:
        message = "the draft frontmatter names no base branch"
        raise OpenRefusedError(message)
    heading, _, rest = body.lstrip().partition("\n")
    title = heading.removeprefix("# ").strip()
    if not heading.startswith("# ") or not title:
        message = "the draft has no H1 title after its frontmatter"
        raise OpenRefusedError(message)
    return Draft(title, rest.strip("\n") + "\n", base, fields.get("draft") is True)


def read_draft(git_dir: Path, commit: str) -> Draft:
    """The draft as the pushed commit holds it.

    ``git_dir`` is the host-owned publication repository, never the run clone,
    so git loads no configuration the sandbox wrote.
    """
    done = subprocess.run(  # nosec B603
        [
            host_tools.resolve("git"),
            "--git-dir",
            str(git_dir),
            "show",
            f"{commit}:{DRAFT}",
        ],
        env={
            "HOME": str(git_dir),
            "PATH": host_tools.search(),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        message = f"the pushed commit has no {DRAFT}"
        raise OpenRefusedError(message)
    return parse_draft(done.stdout)


def open_pull_request(
    git_dir: Path, commit: str, repository: str, branch: str, token: str
) -> int:
    """Create the PR and return its number.

    Raises:
        OpenRefusedError: The draft is missing or malformed, or GitHub
            refuses or cannot be reached.
    """
    draft = read_draft(git_dir, commit)
    _, owner, name = repo.parts(repository)
    try:
        response = httpx.post(
            f"https://api.github.com/repos/{owner}/{name}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "title": draft.title,
                "body": draft.body,
                "head": branch,
                "base": draft.base,
                "draft": draft.draft,
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
