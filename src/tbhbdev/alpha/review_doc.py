"""Parse, validate, and render review documents for the review App."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, cast

from tbhbdev import content
from tbhbdev.alpha.pull_request import parse_frontmatter
from tbhbdev.content_model import normalize

type Tier = Literal[0, 1, 2]
type Verdict = Literal["GO", "NO-GO"]
type FindingClass = Literal["BLOCK", "TECH-DEBT", "SECURITY-DEBT"]
type Variant = Literal["a", "b"]
type _ReviewRow = dict[str, dict[str, object] | str | int] | None
type _Pull = dict[str, dict[str, object]] | None

_HEAD = re.compile(r"[0-9a-f]{40}")
_HEADER = re.compile(
    r"Run: (\S+)\nProvider: (\S+)\nModel: (\S+)\nVariant: ([ab])\n"
    r"Host: (\S+)\nHead: (\S+)\nTier: ([012])\n\n"
)
_FINDING = re.compile(
    r"- (BLOCK|TECH-DEBT|SECURITY-DEBT) `([^\s:`]+):([1-9][0-9]*)` (.+)"
)
# CUE's strings.Fields splits on the Unicode White_Space property.
_CUE_FIELDS = re.compile(
    r"[^\t-\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"
)
_MARKER = re.compile(
    r"^<!-- tbhb-review v1 repo=(?u:(\S+)) pr=([1-9][0-9]{0,11}) "
    r"head=([0-9a-f]{40}) run=(?u:(\S+)) -->$",
    re.ASCII | re.MULTILINE,
)
_PAGE_SIZE = 100
_MARKER_SYNTAX = re.compile(r"<!--\s*tbhb-(?:review|reply)\b", re.IGNORECASE)
_REASON_LIMIT = 200
_VERDICT_WORD = r"(?:NO-GO|GO|APPROVED?|BLOCK|HOLD|REQUEST_CHANGES)"
_WORD_END = r"(?=$|[\s`*.:!?,;–—]|_+(?!\w))"  # noqa: RUF001
_VERDICT_LABEL = r"(?<![^\W_])verdict[`*_]*"
_LABELED_VERDICT = rf"{_VERDICT_LABEL}\s*:[\s`*_]*{_VERDICT_WORD}{_WORD_END}"
_START = r"(?:^\s*(?:>\s*|[-*+]\s+)?|[.!?][`*_]*\s+)"
VERDICT_LINE = re.compile(
    rf"\s*(?:>\s*|[-*+]\s+)?[`*_]*{_LABELED_VERDICT}", re.IGNORECASE
)
SUMMARY_VERDICT = re.compile(
    rf"{_LABELED_VERDICT}|{_VERDICT_LABEL}\s+is\s+[`*_]*{_VERDICT_WORD}{_WORD_END}"
    rf"|{_START}(?:\*\*{_VERDICT_WORD}\*\*|__{_VERDICT_WORD}__)(?=\s|$)"
    rf"|{_START}[`*_]*{_VERDICT_WORD}[`*_]*"
    r"(?:\s*:|[.!?][`*_]*(?=\s|$)|\s*$|\s+because\b)",
    re.IGNORECASE,
)


class ReviewError(ValueError):
    """A review document or operation was refused."""


@dataclass(frozen=True)
class Run:
    name: str
    provider: str
    model: str
    variant: Variant
    host: str


@dataclass(frozen=True)
class Finding:
    class_: FindingClass
    path: str
    line: int
    summary: str


@dataclass(frozen=True)
class ReviewDoc:
    repo: str
    pr: int
    head_sha: str
    tier: Tier
    verdict: Verdict
    summary: str
    run: Run
    findings: tuple[Finding, ...]
    verification: str


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
    marker: Marker
    state: str
    body: str


class Api(Protocol):
    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object: ...


def _visible_summary(summary: str) -> str:
    # The prefix keeps the summary in the same inline context as a finding.
    draft = normalize(
        f"Finding: {summary}",
        object="pr-review",
        # The variant and metadata have no effect on the normalized body.
        variant="review",  # pragma: no mutate
        metadata={},  # pragma: no mutate
    )
    blocks = cast("dict[str, content.Json]", draft["blocks"])  # pragma: no mutate
    visible = cast("str", blocks["preamble"]["visible"])  # pragma: no mutate
    fields: list[str] = _CUE_FIELDS.findall(visible.removeprefix("Finding:"))
    return " ".join(fields)


def _metadata(doc: ReviewDoc, *, visible: bool = False) -> content.Json:
    return {
        "schema": 2,
        "repo": doc.repo,
        "pr": doc.pr,
        "head_sha": doc.head_sha,
        "tier": doc.tier,
        "verdict": doc.verdict,
        "run": asdict(doc.run),
        "findings": [
            {
                "class": item.class_,
                "path": item.path,
                "line": item.line,
                "summary": _visible_summary(item.summary) if visible else item.summary,
            }
            for item in doc.findings
        ],
    }


def _parse_finding(line: str) -> Finding:
    match = _FINDING.fullmatch(line)
    if match is None:
        message = "invalid finding"
        raise ReviewError(message)
    class_, path, number, summary = match.groups()
    try:
        line_number = int(number)
    except ValueError:
        message = "invalid finding line number"
        raise ReviewError(message) from None
    kind = cast("FindingClass", class_)  # pragma: no mutate -- runtime no-op
    return Finding(kind, path, line_number, summary)


def _parse_body(body: str, repo: str, pr: int, verdict: Verdict) -> ReviewDoc:
    sections = re.split(r"^## (.*)$", body, flags=re.MULTILINE)
    if sections[1::2] != ["Summary", "Findings", "Verification"]:
        message = "expected Summary, Findings, and Verification sections in order"
        raise ReviewError(message)
    header, summary, findings, verification = sections[::2]
    match = _HEADER.fullmatch(header.lstrip("\n"))
    if match is None:
        message = "expected the seven run header lines"
        raise ReviewError(message)
    name, provider, model, variant, host, head, tier = match.groups()
    tier_number = int(tier)
    parsed_tier = cast("Tier", tier_number)  # pragma: no mutate -- runtime no-op
    run_variant = cast("Variant", variant)  # pragma: no mutate -- runtime no-op
    return ReviewDoc(
        repo,
        pr,
        head,
        parsed_tier,
        verdict,
        summary.strip("\n"),
        Run(name, provider, model, run_variant, host),
        tuple(_parse_finding(line) for line in findings.strip("\n").splitlines()),
        verification.strip("\n"),
    )


def parse(text: str) -> ReviewDoc:
    """Read a draft with YAML frontmatter and a marker-free Markdown body.

    Raises:
        ReviewError: The draft is malformed or frontmatter disagrees with the body.
    """
    try:
        fields, body = parse_frontmatter(text.replace("\r\n", "\n"))
    except LookupError, OverflowError, RecursionError, ValueError:
        message = "invalid review frontmatter"
        raise ReviewError(message) from None

    # The mapping pattern checks the YAML value at runtime.
    mapping = cast("dict[str, object] | None", fields)  # pragma: no mutate -- no-op
    match mapping:
        case {"schema": int(2), "repo": str(repo), "pr": int(pr), **supplied} if (
            type(pr) is int
        ):
            verdict = supplied.pop("verdict", None)
            if verdict not in ("GO", "NO-GO"):
                message = "expected a GO or NO-GO verdict in frontmatter"
                raise ReviewError(message)
            doc = _parse_body(body, repo, pr, verdict)
        case _:
            message = "expected schema 2 frontmatter with repo and integer pr"
            raise ReviewError(message)
    expected = _metadata(doc)
    for name, value in supplied.items():
        if name not in expected or expected[name] != value:
            message = "unexpected or mismatched review frontmatter field"
            raise ReviewError(message)
    return doc


def _body(doc: ReviewDoc) -> str:
    findings = "".join(
        f"- {item.class_} `{item.path}:{item.line}` {item.summary}\n"
        for item in doc.findings
    )
    if findings:
        findings = f"\n{findings}"
    return (
        f"Run: {doc.run.name}\n"
        f"Provider: {doc.run.provider}\n"
        f"Model: {doc.run.model}\n"
        f"Variant: {doc.run.variant}\n"
        f"Host: {doc.run.host}\n"
        f"Head: {doc.head_sha}\n"
        f"Tier: {doc.tier}\n\n"
        f"## Summary\n\n{doc.summary.rstrip('\n')}\n\n"
        f"## Findings\n{findings}\n"
        f"## Verification\n\n{doc.verification.rstrip('\n')}\n"
    )


def validate(doc: ReviewDoc) -> list[str]:
    """Return refusals from the document cross-checks and the content schema."""
    errors: list[str] = []
    try:
        if _HEAD.fullmatch(doc.head_sha) is None:
            errors.append("head_sha must be 40 lowercase hexadecimal characters")
        has_block = any(item.class_ == "BLOCK" for item in doc.findings)
        if doc.verdict == "GO" and has_block:
            errors.append("GO forbids BLOCK findings")
        if doc.verdict == "NO-GO" and not has_block:
            errors.append("NO-GO requires at least one BLOCK finding")
        if any(map(SUMMARY_VERDICT.search, doc.summary.splitlines())):
            errors.append("Summary must give reasons without stating the verdict")
        body = _body(doc)
        if any(map(VERDICT_LINE.match, body.splitlines())):
            errors.append("keep the final verdict line out of the review body")
        errors.extend(content.check_review(body, metadata=_metadata(doc, visible=True)))
    except Exception as exc:  # noqa: BLE001 -- Validation returns diagnostics.
        errors.append(f"Review validation could not run: {exc}")
    return errors


def render(doc: ReviewDoc) -> str:
    """Render the body to post, with the review marker on its last line."""
    key = marker(doc)
    return (
        f"{_body(doc)}\n<!-- tbhb-review v1 repo={key.repo} pr={key.pr} "
        f"head={key.head_sha} run={key.run} -->\n"
    )


def marker(doc: ReviewDoc) -> Marker:
    """Identify the review by pull request, head, and run name."""
    return Marker(doc.repo, doc.pr, doc.head_sha, doc.run.name)


def parse_marker(body: str) -> Marker | None:
    """Read the last standalone review marker in a posted body."""
    matches = list(_MARKER.finditer(body.replace("\r\n", "\n")))
    if not matches:
        return None
    repo, pr, head, run = matches[-1].groups()
    return Marker(repo, int(pr), head, run)


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
        } if actual_state == state and (key := parse_marker(body)) is not None:
            existing = Existing(review_id, login, key, state, body)
            same_marker = existing.marker == expected
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
    if problems := validate(doc):
        raise ReviewError("\n".join(problems))
    path = f"repos/{doc.repo}/pulls/{doc.pr}"
    check_head(api, path, doc.head_sha)
    event, state = (
        ("APPROVE", "APPROVED")
        if doc.verdict == "GO"
        else ("REQUEST_CHANGES", "CHANGES_REQUESTED")
    )
    existing = find_existing(
        api, doc.repo, doc.pr, doc.head_sha, doc.run.name, author, state=state
    )
    if existing is not None:
        return "exists", existing
    return "posted", send_review(api, path, doc.head_sha, render(doc), event)


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
            if key is not None and (key.repo, key.pr) == (repo, pr):
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
