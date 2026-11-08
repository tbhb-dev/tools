"""Draft and check a pull request review in per-worktree git state."""

import argparse
import re
import sys
from pathlib import Path
from typing import cast

import yaml
from jsonschema import Draft202012Validator

from tbhbdev.alpha import pull_request as pr

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["pr", "commit_id", "event"],
    "properties": {
        "pr": {"type": "integer", "minimum": 1},
        "commit_id": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{40}$",
            "maxLength": 40,
        },
        "event": {"enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"]},
    },
}
LINT_PATH = Path(".github") / "PULL_REQUEST_REVIEW_DRAFT.md"
# Match rumdl's entire MD041 diagnostic so other line-one alerts stay visible.
H1_ALERT = "rumdl: [MD041] First line in file should be a level 1 heading"
HEAD = re.compile(r"[0-9a-fA-F]{40}")
TEMPLATE = """<!--
Write plain prose, one line per paragraph and list item. Cite code as `path:line`.
Remove these guidance comments and any unused optional sections. Write no trailers.
-->

## Verdict

<!-- Required, at most 60 words. The event and one or two reasons for it. -->

## Findings

<!-- Optional, at most 200 words. Blocking issues only, one list item each,
with `path:line` and what has to change. REQUEST_CHANGES requires findings. -->

## Suggestions

<!-- Optional, at most 120 words. Non-blocking improvements. -->

## Verification

<!-- Required, at most 100 words. What the reviewer read or ran, and the CI
result it relied on. -->
"""
SECTIONS = [
    pr.Section("Verdict", required=True, max_words=60),
    pr.Section("Findings", required=False, max_words=200),
    pr.Section("Suggestions", required=False, max_words=120),
    pr.Section("Verification", required=True, max_words=100),
]


def draft_path(number: int) -> Path:
    """The review's file under this worktree's git directory."""
    code, directory = pr.git("rev-parse", "--git-dir")
    if code != 0:
        message = "review drafts require a git worktree"
        raise SystemExit(message)
    return Path(directory).resolve() / "tbhb-alpha" / f"review-{number}.md"


def fetch_head(number: int) -> str:
    """Read only the PR's head SHA through the agent's gh.

    Raises:
        SystemExit: gh fails or does not return a head SHA.
    """
    done = pr.run(
        [pr.find_tool(pr.GH), "pr", "view", str(number), "--json", "headRefOid"]
    )
    if done.returncode != 0:
        message = f"gh pr view failed: {' '.join(done.stderr.split())}"
        raise SystemExit(message)
    data = pr.gh_data(done)
    head = data.get("headRefOid") if isinstance(data, dict) else None
    if not isinstance(head, str) or HEAD.fullmatch(head) is None:
        message = "gh pr view did not return a 40-hex headRefOid"
        raise SystemExit(message)
    return head


def refresh(text: str, head: str) -> str:
    """Replace only commit_id, preserving the rest of an existing draft.

    Raises:
        SystemExit: The frontmatter has no commit_id field to replace.
    """
    normalized = text.replace("\r\n", "\n")
    try:
        data, _ = pr.parse_frontmatter(normalized)
    except pr.FrontmatterError as exc:
        message = f"frontmatter: {exc}"
        raise SystemExit(message) from exc
    if not isinstance(data, dict) or "commit_id" not in data:
        message = "frontmatter needs a commit_id field to refresh"
        raise SystemExit(message)
    # A mapping from parse_frontmatter has a matching block and mapping node.
    matched = pr.FRONTMATTER.match(normalized)
    match = cast("re.Match[str]", matched)  # pragma: no mutate -- runtime no-op
    # Values already passed the safe parser; compose only reads source positions.
    composed: object = yaml.compose(match["yaml"])
    node = cast("yaml.MappingNode", composed)  # pragma: no mutate -- runtime no-op
    value = next(
        value for key, value in reversed(node.value) if key.value == "commit_id"
    )
    raw = text.splitlines(keepends=True)
    # Node marks are relative to the YAML, which starts after the opening delimiter.
    start = sum(map(len, raw[: value.start_mark.line + 1])) + value.start_mark.column
    end = sum(map(len, raw[: value.end_mark.line + 1])) + value.end_mark.column
    end = start + len(text[start:end].rstrip())
    replacement = f"'{head}'"
    if text[start - 1] == ":":
        replacement = " " + replacement
    return text[:start] + replacement + text[end:]


def draft(number: int, *, force: bool) -> Path:
    """Create a skeleton, or refresh its head when explicitly requested."""
    path = draft_path(number)
    if path.exists() and not force:
        return path
    head = fetch_head(number)
    text = (
        refresh(path.read_text(newline=""), head)
        if path.exists()
        else f"---\npr: {number}\ncommit_id: '{head}'\nevent:\n---\n{TEMPLATE}"
    )
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def check_text(text: str, number: int) -> list[pr.Problem]:
    """Validate review metadata and its title-free body without reading GitHub."""
    try:
        data, body = pr.parse_frontmatter(text)
    except pr.FrontmatterError as exc:
        return [(1, f"frontmatter: {exc}")]
    if data is None or data == {}:
        return [(1, "review frontmatter is required and must not be empty")]
    offset = text.count("\n") - body.count("\n")
    schema = {**SCHEMA, "allOf": [{"properties": {"pr": {"const": number}}}]}
    problems = pr.meta_problems(data, offset, text, Draft202012Validator(schema))
    lines, scanned = pr.scan(body.splitlines(), offset + 1)
    problems += scanned
    anchor = pr.Line(
        offset + 1,
        "",  # pragma: no mutate -- body_problems reads only the anchor's number
        "",  # pragma: no mutate -- body_problems reads only the anchor's number
        code=False,  # pragma: no mutate -- body_problems reads only the number
    )
    problems += [
        (line, "a review must not contain an H1 title")
        if why == "only one H1 is allowed: the title"
        else (line, why)
        for line, why in pr.body_problems(lines, anchor, SECTIONS)
    ]
    problems += pr.ref_problems(lines)
    if isinstance(data, dict) and data.get("event") == "REQUEST_CHANGES":
        problems += findings_problems(lines, anchor.no)
    rendered = body.splitlines()
    first_content = next(
        (index for index, line in enumerate(rendered, 1) if line.strip()), None
    )
    if first_content is not None:
        problems += pr.lint(
            rendered,
            offset + 1,
            path=LINT_PATH,
            drop=frozenset({(first_content, H1_ALERT)}),
        )
    return sorted(set(problems))


def findings_problems(lines: list[pr.Line], first: int) -> list[pr.Problem]:
    """A request for changes needs a non-empty Findings section."""
    _, blocks, _ = pr.split_blocks(lines)
    if any(
        block.heading == "Findings" and not all(map(pr.is_blank, block.lines))
        for block in blocks
    ):
        return []
    return [(first, "REQUEST_CHANGES requires non-empty Findings")]


def main(argv: list[str], prog: str) -> int:
    """Run the review draft or check command."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("draft")
    create.add_argument("number", type=int)
    create.add_argument("--force", action="store_true")
    sub.add_parser("check").add_argument("number", type=int)
    args = parser.parse_args(argv)
    if args.number < 1:
        parser.error("PR number must be at least 1")
    if args.command == "draft":
        print(draft(args.number, force=args.force))
        return 0
    path = draft_path(args.number)
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as exc:
        problems = [(1, f"cannot read draft: {exc}")]
    else:
        problems = check_text(text, args.number)
    for line, why in problems:
        print(f"{path}:{line}: {why}", file=sys.stderr)
    return 1 if problems else 0
