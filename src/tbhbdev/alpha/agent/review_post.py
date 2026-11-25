"""Post a review run's verdict as a GitHub review, host-side, with the run's token.

The sandbox only writes the draft file. Run-host validates it, adds the run's
identity to the body and posts it after the run ends. A run can comment or
request changes; approval stays with a human-gated reviewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx

from tbhbdev.alpha import pull_request, review
from tbhbdev.alpha.agent import credentials, runfiles
from tbhbdev.alpha.agent.decisions import repo

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host import Prepared, Run

ALLOWED_EVENTS = frozenset({"COMMENT"})
DRAFT_NAME = re.compile(r"review-(\d+)\.md")
DRAFT_LIMIT = 64 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class PostRefusedError(ValueError):
    """The draft or the API refused; the message is safe to log."""


@dataclass(frozen=True)
class Submission:
    """One review to post: the draft text and what the post must match."""

    repository: str
    number: int
    text: str
    commit: str
    stamp: str


def find_draft(workspace: Path) -> tuple[int, str]:
    """The run's one review draft as (pull request number, text).

    Raises:
        PostRefusedError: The run left no draft, or more than one.
    """
    directory = workspace / ".git" / "tbhb-alpha"
    found = sorted(
        (int(match[1]), path)
        for path in directory.glob("review-*.md")
        if (match := DRAFT_NAME.fullmatch(path.name))
    )
    if len(found) != 1:
        message = f"expected one review draft, found {len(found)}"
        raise PostRefusedError(message)
    number, path = found[0]
    return number, runfiles.read_owned(path, DRAFT_LIMIT, "review draft")


def validate(sub: Submission) -> tuple[dict[str, object], str]:
    """Check the draft host-side and return (frontmatter, body).

    Check the schema, template sections and frontmatter key uniqueness without
    the prose linters, which are unavailable on run hosts.

    Raises:
        PostRefusedError: The draft fails a check, names another head than the
            run reviewed, or asks for an event a run can't post.
    """
    try:
        problems = review.check_text(sub.text, sub.number, lint=False)
    except RecursionError as exc:
        message = "the draft frontmatter is nested too deeply"
        raise PostRefusedError(message) from exc
    if problems:
        line, why = problems[0]
        message = f"the draft fails its checks: line {line}: {why}"
        raise PostRefusedError(message)
    meta, body = pull_request.parse_frontmatter(sub.text)
    fields = cast("dict[str, object]", meta)
    if fields["event"] not in ALLOWED_EVENTS:
        message = f"a run can't post a {fields['event']} review"
        raise PostRefusedError(message)
    if str(fields["commit_id"]).lower() != sub.commit.lower():
        message = "the draft's commit_id isn't the commit the run reviewed"
        raise PostRefusedError(message)
    return fields, body.strip("\n") + "\n"


def api(
    method: str, url: str, token: str, payload: dict[str, str] | None = None
) -> object:
    """One GitHub API call; any 2xx is success.

    Raises:
        PostRefusedError: GitHub refuses or can't be reached.
    """
    try:
        response = httpx.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json=payload,
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        message = f"the {method} request failed: {type(exc).__name__}"
        raise PostRefusedError(message) from exc
    if not response.is_success:
        message = f"GitHub refused the {method} with status {response.status_code}"
        raise PostRefusedError(message)
    try:
        return response.json() if response.content else None
    except ValueError:
        # A 2xx whose body isn't JSON still means the request succeeded.
        return None


def pull_head(data: object) -> str | None:
    head = data.get("head") if isinstance(data, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) else None


def create(sub: Submission, token: str) -> int | None:
    """Validate, then post once; return the review id, or None if GitHub sent none.

    Raises:
        PostRefusedError: A check fails, the head moved, or GitHub refuses.
    """
    fields, body = validate(sub)
    _, owner, name = repo.parts(sub.repository)
    base = f"https://api.github.com/repos/{owner}/{name}/pulls/{sub.number}"
    if pull_head(api("GET", base, token)) != sub.commit:
        message = "the pull request head moved since the run judged it"
        raise PostRefusedError(message)
    payload = {
        "commit_id": sub.commit,
        "body": f"{body}\n---\n{sub.stamp}\n",
        "event": str(fields["event"]),
    }
    data = api("POST", f"{base}/reviews", token, payload)
    created = data.get("id") if isinstance(data, dict) else None
    return created if isinstance(created, int) else None


def stamp(run: Run) -> str:
    """The run's identity and the judged head, as the body's last line."""
    request = run.request
    return (
        f"Run {run.files.name}, {request.harness.value} {request.model}, "
        f"judged head {request.commit}."
    )


def posted(run: Run, prepared: Prepared) -> bool:
    """Post the run's review and record it; False when it was not posted."""
    if run.files.path("review").exists():
        return False
    workspace = Path(prepared.paths.worktree)
    try:
        number, text = find_draft(workspace)
        token = credentials.review_post_token(run.request, run.settings)
        if token is None:
            message = "reviews App token unavailable"
            raise PostRefusedError(message)  # noqa: TRY301
        sub = Submission(
            run.request.repo, number, text, run.request.commit or "", stamp(run)
        )
        identifier = create(sub, token)
    except PostRefusedError as refusal:
        run.files.write("log", f"review not posted: {refusal}\n")
        return False
    run.files.write("review", f"{run.request.repo}#{number} {identifier or 'unknown'}")
    return True
