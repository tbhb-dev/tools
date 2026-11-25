"""Parse, validate, and render review documents for the review App."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal, cast

from tbhbdev.alpha.pull_request import parse_frontmatter

type Tier = Literal[0, 1, 2]
type Verdict = Literal["GO", "NO-GO"]
type FindingClass = Literal["BLOCK", "TECH-DEBT", "SECURITY-DEBT"]
type Variant = Literal["a", "b"]

_HEADER = re.compile(
    r"Run: (\S+)\nProvider: (\S+)\nModel: (\S+)\nVariant: ([ab])\n"
    r"Host: (\S+)\nHead: (\S+)\nTier: ([012])\n\n"
)
_FINDING = re.compile(
    r"- (BLOCK|TECH-DEBT|SECURITY-DEBT) `([^\s:`]+):([1-9][0-9]*)` (.+)"
)
_MARKER = re.compile(
    r"^<!-- tbhb-review v1 repo=(?u:(\S+)) pr=([1-9][0-9]{0,11}) "
    r"head=([0-9a-f]{40}) run=(?u:(\S+)) -->$",
    re.ASCII | re.MULTILINE,
)


class ReviewError(ValueError):
    """The review draft cannot be parsed."""


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


def _metadata(doc: ReviewDoc) -> dict[str, object]:
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
                "summary": item.summary,
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


def _parse_body(body: str, repo: str, pr: int) -> ReviewDoc:
    sections = re.split(r"^## (.*)$", body, flags=re.MULTILINE)
    if sections[1::2] != ["Verdict", "Findings", "Verification"]:
        message = "expected Verdict, Findings, and Verification sections in order"
        raise ReviewError(message)
    header, verdict, findings, verification = sections[::2]
    match = _HEADER.fullmatch(header.lstrip("\n"))
    if match is None:
        message = "expected the seven run header lines"
        raise ReviewError(message)
    name, provider, model, variant, host, head, tier = match.groups()
    verdict = verdict.strip()
    if verdict not in ("GO", "NO-GO"):
        message = "expected a GO or NO-GO verdict"
        raise ReviewError(message)
    tier_number = int(tier)
    parsed_tier = cast("Tier", tier_number)  # pragma: no mutate -- runtime no-op
    run_variant = cast("Variant", variant)  # pragma: no mutate -- runtime no-op
    return ReviewDoc(
        repo,
        pr,
        head,
        parsed_tier,
        verdict,
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
            doc = _parse_body(body, repo, pr)
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
        f"## Verdict\n\n{doc.verdict}\n\n"
        f"## Findings\n{findings}\n"
        f"## Verification\n\n{doc.verification.rstrip('\n')}\n"
    )


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
    return existing.state == "DISMISSED" or existing.body.startswith("SUPERSEDED:")
