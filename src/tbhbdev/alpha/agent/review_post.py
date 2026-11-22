"""Post a review run's verdict as a GitHub review, host-side, with the run's token.

The sandbox only writes the draft file. Run-host validates it, adds the run's
identity to the body and posts it after the run ends. A run can comment or
request changes; approval stays with a human-gated reviewer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx

from tbhbdev.alpha import pull_request, review, review_policy
from tbhbdev.alpha.agent import credentials, runfiles
from tbhbdev.alpha.agent.decisions import repo

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host import Prepared, Run

POSTABLE = frozenset({"COMMENT", "REQUEST_CHANGES"})
DRAFT_NAME = re.compile(r"review-(\d+)\.md")
DRAFT_LIMIT = 64 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class PostRefusedError(ValueError):
    """The draft or the API refused; the message is safe to log."""


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


def validate(text: str, number: int, commit: str | None) -> tuple[str, str]:
    """Check the draft host-side and return (event, body).

    The prose linters aren't on run hosts, so the run's own `review check`
    stands for them. Everything the schema and the template sections cover is
    checked again here.

    Raises:
        PostRefusedError: The draft fails a check, names another head than the
            run reviewed, or asks for an event a run can't post.
    """
    problems = review.check_text(text, number, lint=False)
    if problems:
        line, why = problems[0]
        message = f"the draft fails its checks: line {line}: {why}"
        raise PostRefusedError(message)
    meta, body = pull_request.parse_frontmatter(text)
    fields = cast("dict[str, object]", meta)
    event = fields["event"]
    if event not in POSTABLE:
        message = f"a run can't post a {event} review"
        raise PostRefusedError(message)
    if commit is None or str(fields["commit_id"]).lower() != commit.lower():
        message = "the draft's commit_id isn't the commit the run reviewed"
        raise PostRefusedError(message)
    return event, body.strip("\n") + "\n"


def stamp(body: str, run: Run) -> str:
    """Add the run's identity and the judged head to the review body."""
    request = run.request
    return (
        f"{body}\n---\n"
        f"Run {run.files.name}, {request.harness.value} {request.model}, "
        f"judged head {request.commit}.\n"
    )


def post(repository: str, number: int, review: dict[str, str], token: str) -> int:
    """Create the review and return its id.

    Raises:
        PostRefusedError: GitHub refuses or can't be reached.
    """
    _, owner, name = repo.parts(repository)
    try:
        response = httpx.post(
            f"https://api.github.com/repos/{owner}/{name}/pulls/{number}/reviews",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json=review,
            timeout=TIMEOUT,
        )
        identifier = cast("dict[str, int]", response.json()).get("id")
    except (httpx.HTTPError, ValueError) as exc:
        message = f"the review request failed: {type(exc).__name__}"
        raise PostRefusedError(message) from exc
    if response.status_code != httpx.codes.OK or not isinstance(identifier, int):
        message = f"GitHub refused the review with status {response.status_code}"
        raise PostRefusedError(message)
    return identifier


def verdict(run: Run, workspace: Path) -> tuple[int, str, str, str]:
    """Everything the post needs, as (number, event, stamped body, token).

    Raises:
        PostRefusedError: A check fails or the token can't be minted.
    """
    number, text = find_draft(workspace)
    event, body = validate(text, number, run.request.commit)
    findings = review_policy.check(workspace)
    if findings:
        first = findings[0]
        message = f"{first.path}: {first.rule}: {first.message}"
        raise PostRefusedError(message)
    token = credentials.review_post_token(run.request, run.settings)
    if token is None:
        message = "reviews App token unavailable"
        raise PostRefusedError(message)
    return number, event, stamp(body, run), token


def posted(run: Run, prepared: Prepared) -> bool:
    """Post the run's review and record it; False when it was not posted."""
    try:
        number, event, body, token = verdict(run, Path(prepared.paths.worktree))
        commit = cast("str", run.request.commit)
        review = {"commit_id": commit, "body": body, "event": event}
        identifier = post(run.request.repo, number, review, token)
    except (PostRefusedError, OSError, ValueError) as refusal:
        run.files.write("log", f"review not posted: {refusal}\n")
        return False
    run.files.write("review", f"{run.request.repo}#{number} {identifier}")
    return True
