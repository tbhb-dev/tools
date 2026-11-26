"""Parse, validate, and render review documents for the review App."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, cast

from tbhbdev import content
from tbhbdev.alpha import pull_request as pr

type Tier = Literal[0, 1, 2]
type Verdict = Literal["GO", "NO-GO"]
type Variant = Literal["a", "b"]
type _ReviewRow = dict[str, dict[str, object] | str | int] | None
type _Pull = dict[str, dict[str, object]] | None

_HEAD = re.compile(r"[0-9a-f]{40}")
_MARKER = re.compile(
    r"^<!-- tbhb-review v1 repo=(?u:(\S+)) pr=([1-9][0-9]{0,11}) "
    r"head=([0-9a-f]{40}) run=(?u:(\S+)) -->$",
    re.ASCII | re.MULTILINE,
)
_MARKER_V2 = re.compile(r"^<!-- tbhb-review v2 ([0-9a-f]{64}) -->$", re.MULTILINE)
_PAGE_SIZE = 100
_MARKER_SYNTAX = re.compile(r"<!--\s*tbhb-(?:review|reply)\b", re.IGNORECASE)
_REASON_LIMIT = 200


class ReviewError(ValueError):
    """A review document or operation was refused."""


@dataclass(frozen=True)
class ReviewDoc:
    repo: str
    pr: int
    head_sha: str
    run: str
    verdict: Verdict
    body: str
    metadata: content.Json


@dataclass(frozen=True)
class Marker:
    """Identify one run's review of one pull request head."""

    repo: str
    pr: int
    head_sha: str
    run: str


@dataclass(frozen=True)
class Existing:
    id: int
    author: str
    marker: Marker | str
    state: str
    body: str


class Api(Protocol):
    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object: ...


LINT_PATH = Path(".github") / "PULL_REQUEST_REVIEW_DRAFT.md"


def lint_post(body: str, *, worktree: Path | None = None) -> list[str]:
    """Run the repository's Vale rules on the review prose before posting."""
    try:
        problems = pr.lint(
            body.splitlines(),
            1,
            path=LINT_PATH,
            vale_only=True,
            worktree=worktree,
            drop=frozenset(),
        )
    except (OSError, SystemExit) as exc:
        return [f"Vale could not run: {exc}"]
    return [
        f"line {line}: {message}"
        for line, message in problems
        if not message.startswith(("vale Vale.Spelling:", "vale Vale.Terms:"))
    ]


def parse(text: str) -> ReviewDoc:
    """Read a review document with run details in YAML frontmatter."""
    try:
        fields, body = pr.parse_frontmatter(text.replace("\r\n", "\n"))
    except LookupError, OverflowError, RecursionError, ValueError:
        message = "invalid review frontmatter"
        raise ReviewError(message) from None
    match cast("_ReviewRow", fields):
        case {
            "repo": str(repo),
            "pr": int(number),
            "head_sha": str(head),
            "run": {"name": str(run)},
            "verdict": ("GO" | "NO-GO") as verdict,
        } if type(number) is int:
            return ReviewDoc(
                repo, number, head, run, verdict, body, cast("content.Json", fields)
            )
        case _:
            message = "expected repo, integer pr, head_sha, run name, and GO or NO-GO"
            raise ReviewError(message)


def marker_id(repo: str, number: int, run: str) -> str:
    """Hash the run key for deduplication within the pull request."""
    return sha256(f"{repo}#{number} {run}".encode()).hexdigest()


def render(doc: ReviewDoc) -> str:
    """Render the body to post with its deduplication marker."""
    marker = marker_id(doc.repo, doc.pr, doc.run)
    prose = doc.body.strip("\n")
    return f"{prose}\n\n<!-- tbhb-review v2 {marker} -->\n"


def parse_marker(body: str) -> Marker | str | None:
    """Read a current or legacy review marker."""
    text = body.replace("\r\n", "\n")
    if matched := _MARKER_V2.fullmatch(text.rstrip().rpartition("\n")[2]):
        return matched[1]
    matches = list(_MARKER.finditer(text))
    if not matches:
        return None
    repo, number, head, run = matches[-1].groups()
    return Marker(repo, int(number), head, run)


def is_dismissed(existing: Existing) -> bool:
    """Check whether GitHub dismissed the review or the App superseded it."""
    return _dismissed(existing.state, existing.body)


def _dismissed(state: str, body: str) -> bool:
    return state == "DISMISSED" or body.startswith("SUPERSEDED:")


def _active_id(entry: object, expected: Marker, author: str, state: str) -> int | None:
    # The mapping pattern checks the API response at runtime.
    match cast("_ReviewRow", entry):  # pragma: no mutate -- cast is a runtime no-op
        case {
            "id": int(review_id),
            "user": {"login": str(login)},
            "body": str(body),
            "state": str(actual_state),
            "commit_id": str(head),
        } if actual_state == state and (key := parse_marker(body)) is not None:
            existing = Existing(review_id, login, key, state, body)
            same_marker = (
                existing.marker
                in (expected, marker_id(expected.repo, expected.pr, expected.run))
                and head == expected.head_sha
            )
            same_author = existing.author.casefold() == author.casefold()
            if same_marker and same_author and not is_dismissed(existing):
                return existing.id
        case _:  # pragma: no mutate -- falls through to the same return
            pass
    return None


def find_existing(  # noqa: PLR0913, PLR0917
    api: Api, repo: str, pr: int, head_sha: str, run: str, author: str, *, state: str
) -> int | None:
    """Page through GET /repos/{repo}/pulls/{pr}/reviews?per_page=100.

    Return the ID of the newest active review with the matching author, marker,
    and decision state, or None if none match.
    """
    expected = Marker(repo, pr, head_sha, run)
    newest = None
    page = 1
    while True:
        path = f"repos/{repo}/pulls/{pr}/reviews?per_page={_PAGE_SIZE}&page={page}"
        reviews = api.request("GET", path)
        if not isinstance(reviews, list):
            message = "expected a list of reviews"
            raise ReviewError(message)
        # GitHub returns reviews in chronological order.
        entries = cast("list[object]", reviews)  # pragma: no mutate -- runtime no-op
        for entry in entries:
            if (matched := _active_id(entry, expected, author, state)) is not None:
                newest = matched
        if len(reviews) < _PAGE_SIZE:
            return newest
        page += 1


def check_head(api: Api, path: str, commit: str) -> None:
    """Refuse a draft for a head that is no longer current."""
    live = api.request("GET", path)
    match cast("_Pull", live):  # pragma: no mutate -- runtime no-op
        case {"head": {"sha": head}} if head == commit:
            return
        case _:
            message = "pull request head has moved"
            raise ReviewError(message)


def send_review(api: Api, path: str, commit: str, body: str, event: str) -> int | None:
    """Submit the body with its review event and return the ID if provided."""
    result = api.request(
        "POST",
        f"{path}/reviews",
        {"commit_id": commit, "body": body, "event": event},
    )
    match cast("dict[str, object] | None", result):  # pragma: no mutate -- cast
        case {"id": int(review_id)}:
            return review_id
        case _:
            return None


def post(
    api: Api, doc: ReviewDoc, *, author: str
) -> tuple[Literal["posted", "exists"], int | None]:
    """Post the schema 2 verdict as a GitHub review event after validation.

    Return ("exists", id) if the author already posted the matching decision.
    Accept the race between concurrent posts without a lock.
    """
    body = render(doc)
    problems = content.check_review(body, metadata=doc.metadata, posted=True)
    if problems := problems or lint_post(doc.body):
        raise ReviewError("\n".join(problems))
    path = f"repos/{doc.repo}/pulls/{doc.pr}"
    check_head(api, path, doc.head_sha)
    event, state = (
        ("APPROVE", "APPROVED")
        if doc.verdict == "GO"
        else ("REQUEST_CHANGES", "CHANGES_REQUESTED")
    )
    existing = find_existing(
        api, doc.repo, doc.pr, doc.head_sha, doc.run, author, state=state
    )
    if existing is not None:
        return "exists", existing
    return "posted", send_review(api, path, doc.head_sha, body, event)


def dismiss(
    api: Api,
    repo: str,
    pr: int,
    review_id: int,
    reason: str,
) -> None:
    """Edit a COMMENT review to prefix its body with `SUPERSEDED: <reason>`.

    Require a plain-text reason of at most 200 characters with no markers.
    Preserve the original marker.
    """
    one_line = reason.splitlines() == [reason]
    if not one_line or len(reason) > _REASON_LIMIT or _MARKER_SYNTAX.search(reason):
        message = "reason must be one line of at most 200 characters without markers"
        raise ReviewError(message)
    path = f"repos/{repo}/pulls/{pr}/reviews/{review_id}"
    response = api.request("GET", path)
    match cast("dict[str, object] | None", response):  # pragma: no mutate -- cast
        case {"body": str(body), "state": ("COMMENTED" | "DISMISSED") as state}:
            key = parse_marker(body)
            if isinstance(key, str) or (
                isinstance(key, Marker) and (key.repo, key.pr) == (repo, pr)
            ):
                if not _dismissed(state, body):
                    body = f"SUPERSEDED: {reason}\n{body}"
                    api.request("PUT", path, {"body": body})
                return
        case _:  # pragma: no mutate -- falls through to the same refusal
            pass
    message = "expected a COMMENT review marked for this pull request"
    raise ReviewError(message)


def reply(
    api: Api,
    repo: str,
    pr: int,
    review_id: int,
    text: str,
) -> int:
    """Post a PR issue comment and return its ID.

    Use the review's head to format `Re review <id> (head <sha8>): <text>`
    followed by a separate `<!-- tbhb-reply v1 review=<id> -->` marker line.

    """
    if _MARKER_SYNTAX.search(text):
        message = "reply text must not contain markers"
        raise ReviewError(message)
    review = api.request("GET", f"repos/{repo}/pulls/{pr}/reviews/{review_id}")
    match cast("dict[str, object] | None", review):  # pragma: no mutate -- cast
        case {"commit_id": str(head)} if _HEAD.fullmatch(head):
            body = f"Re review {review_id} (head {head[:8]}): {text}\n"
        case _:
            message = "review lacks a 40-hex commit ID"
            raise ReviewError(message)
    body += f"<!-- tbhb-reply v1 review={review_id} -->"
    response = api.request("POST", f"repos/{repo}/issues/{pr}/comments", {"body": body})
    match cast("dict[str, object] | None", response):  # pragma: no mutate -- cast
        case {"id": int(comment_id)}:
            return comment_id
        case _:
            message = "missing reply comment ID"
            raise ReviewError(message)
