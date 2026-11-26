"""Draft and check a pull request review in per-worktree git state."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import cast

import yaml
from jsonschema import Draft202012Validator

from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha import review_doc, review_policy, review_threads

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

## Summary

<!-- Required, at most 60 words. Give the reasons in one or two sentences.
The event carries the verdict. -->

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
    pr.Section("Summary", required=True, max_words=60),
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


def check_text(text: str, number: int, *, lint: bool = True) -> list[pr.Problem]:
    """Validate review metadata and its title-free body without reading GitHub.

    Run hosts lack the prose linters, so they pass ``lint=False``.
    """
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
    _, blocks, _ = pr.split_blocks(lines)
    problems += verdict_problems(lines, blocks)
    if isinstance(data, dict) and data.get("event") == "REQUEST_CHANGES":
        problems += findings_problems(blocks, anchor.no)
    rendered = body.splitlines()
    first_content = next(
        (index for index, line in enumerate(rendered, 1) if line.strip()), None
    )
    if lint and first_content is not None:
        problems += pr.lint(
            rendered,
            offset + 1,
            path=LINT_PATH,
            drop=frozenset({(first_content, H1_ALERT)}),
        )
    return sorted(set(problems))


def verdict_problems(lines: list[pr.Line], blocks: list[pr.Block]) -> list[pr.Problem]:
    """Keep verdict statements out of the Summary and final lines out of the body."""
    problems = [
        (line.no, "Summary must give reasons without stating the verdict")
        for block in blocks
        if block.heading == "Summary"
        for line in block.lines
        if review_doc.SUMMARY_VERDICT.search(line.text)
    ]
    return problems + [
        (line.no, "keep the final verdict line out of the review body")
        for line in lines
        if review_doc.VERDICT_LINE.match(line.raw)
    ]


def findings_problems(blocks: list[pr.Block], first: int) -> list[pr.Problem]:
    """A request for changes needs a non-empty Findings section."""
    if any(
        block.heading == "Findings" and not all(map(pr.is_blank, block.lines))
        for block in blocks
    ):
        return []
    return [(first, "REQUEST_CHANGES requires non-empty Findings")]


class GhApi:
    # Posts as the agent's own account, separate from the review App in
    # agent/review_post.py. Run-host does not use these commands.
    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object:
        args = [pr.find_tool(pr.GH), "api", "--method", method, path]
        stdin = ""
        if payload is not None:
            args += ["--input", "-"]
            stdin = json.dumps(payload)
        if (done := pr.run(args, stdin=stdin)).returncode != 0:
            raise review_doc.ReviewError(done.stderr.strip())
        try:
            return json.loads(done.stdout or "null")
        except json.JSONDecodeError as exc:
            message = "GitHub returned invalid JSON"
            raise review_doc.ReviewError(message) from exc


def api_author(api: review_doc.Api) -> str:
    user = api.request("GET", "user")
    match cast("dict[str, object] | None", user):  # pragma: no mutate -- cast
        case {"login": str(login)} if login:
            return login
        case _:
            message = "GitHub did not return a login"
            raise review_doc.ReviewError(message)


def post_draft(number: int, author: str | None) -> int:
    path = draft_path(number).with_suffix(".doc.md")
    try:
        doc = review_doc.parse(path.read_text())
        if doc.pr != number:
            message = f"draft must name pull request {number}"
            raise review_doc.ReviewError(message)
        repo = pr.origin_repository()
        if doc.repo != repo:
            message = f"draft repo {doc.repo} differs from origin repo {repo}"
            raise review_doc.ReviewError(message)
        api = GhApi()
        status, review_id = review_doc.post(api, doc, author=author or api_author(api))
    except (OSError, SystemExit, ValueError) as exc:
        for problem in str(exc).splitlines():
            print(f"{path}: {problem}", file=sys.stderr)
        return 1
    print(status if review_id is None else f"{status} {review_id}")
    return 0


def edit_review(args: argparse.Namespace) -> int:
    try:
        target = (GhApi(), pr.origin_repository(), args.number, args.review_id)
        if args.command == "dismiss":
            review_doc.dismiss(*target, args.reason)
            result = f"superseded {args.review_id}"
        else:
            comment = review_doc.reply(*target, args.text)
            result = f"replied {comment}"
    except (OSError, SystemExit, review_doc.ReviewError) as exc:
        print(f"review {args.review_id}: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


def thread_command(args: argparse.Namespace) -> int:
    try:
        target = (GhApi(), pr.origin_repository(), args.number)
        if args.command == "threads":
            for row in review_threads.list_rows(*target, unresolved=args.unresolved):
                print(row)
        else:
            print(review_threads.update(*target, args.thread, args.command))
    except (OSError, SystemExit, review_doc.ReviewError) as exc:
        print(f"PR {args.number} {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str], prog: str) -> int:
    """Run a review command."""
    if argv[:1] == ["policy"]:
        return review_policy.main(argv[1:], f"{prog} policy")
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("draft")
    create.add_argument("number", type=int)
    create.add_argument("--force", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("number", type=int)
    check.add_argument("--no-lint", action="store_true", help="skip prose linters")
    sub.add_parser("policy", help="check a worktree's review policy files")
    post = sub.add_parser("post")
    post.add_argument("number", type=int)
    post.add_argument("--author")
    for command, option in (("dismiss", "--reason"), ("reply", "--text")):
        edit = sub.add_parser(command)
        edit.add_argument("number", type=int)
        edit.add_argument("review_id", type=int)
        edit.add_argument(option, required=True)
    threads = sub.add_parser("threads", help="list pull request review threads")
    threads.add_argument("number", type=int)
    threads.add_argument("--unresolved", action="store_true")
    for command in ("resolve", "unresolve"):
        thread = sub.add_parser(command, help=f"{command} a pull request review thread")
        thread.add_argument("number", type=int)
        thread.add_argument("thread", help="thread ID or review comment database ID")
    args = parser.parse_args(argv)
    if args.number < 1:
        parser.error("PR number must be at least 1")
    if args.command == "post":
        return post_draft(args.number, args.author)
    if args.command in {"threads", "resolve", "unresolve"}:
        return thread_command(args)
    if args.command in {"dismiss", "reply"}:
        if args.review_id < 1:
            parser.error("review ID must be at least 1")
        return edit_review(args)
    if args.command == "draft":
        print(draft(args.number, force=args.force))
        return 0
    path = draft_path(args.number)
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as exc:
        problems = [(1, f"cannot read draft: {exc}")]
    else:
        problems = check_text(text, args.number, lint=not args.no_lint)
    for line, why in problems:
        print(f"{path}:{line}: {why}", file=sys.stderr)
    return 1 if problems else 0
