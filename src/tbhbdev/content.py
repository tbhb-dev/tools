"""Shared GitHub content validation and writes for plugins."""

import json
import os
import re
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from tbhbdev import content_vet, cue_runtime
from tbhbdev.alpha import secret_scan
from tbhbdev.alpha.pull_request import find_tool, root
from tbhbdev.content_model import normalize

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type Json = dict[str, object]

CheckResult = content_vet.CheckResult
Finding = content_vet.Finding
check = content_vet.check
check_close = content_vet.check_close
format_refusal = content_vet.format_refusal
guide = content_vet.guide

_RECORD_SECTIONS = ("Context", "Decision", "Consequences")
_RECORD_OPTIONS = "Options considered"
_RECORD_SECTION_ORDERS = {
    _RECORD_SECTIONS,
    ("Context", "Decision", _RECORD_OPTIONS, "Consequences"),
}
_RECORD_WORD_LIMITS = {"Context": 150, "Decision": 120, "Consequences": 150}
_RECORD_TOTAL_LIMIT = 450
_RECORD_OPTION_LIMIT = 5
_RECORD_OPTION_WORD_LIMIT = 25
_RECORD_TITLE_MINIMUM = 4
# A closing quote must end a sentence, end the line, or precede attribution.
_RECORD_QUOTE = re.compile(
    r"""^[^\S\n]*(?:"[^"\n]*"|'[^\n]*?'|“[^”\n]*”|‘[^\n]*?’)"""  # noqa: RUF001
    r"""(?:(?<=[.!?,]["'”’])|(?=[^\S\n]*(?:$|[,.;!?—–-])))""",  # noqa: RUF001
    re.MULTILINE,
)


# Casts narrow the normalizer's JSON model for the type checker and do not
# change runtime values. Mutations to those casts can be equivalent.
def _record_words(value: Json) -> int:
    metrics = cast("Json", value["metrics"])  # pragma: no mutate
    visible = cast("dict[str, int]", metrics["visible"])  # pragma: no mutate
    return visible["words"]


def _record_paragraphs(blocks: list[Json], refusals: list[str]) -> None:
    for block in blocks:
        if block["kind"] != "paragraph":
            children = block.get("items", block.get("blocks", []))
            _record_paragraphs(
                cast("list[Json]", children),  # pragma: no mutate
                refusals,
            )
            continue
        span = cast("dict[str, dict[str, int]]", block["span"])  # pragma: no mutate
        line = span["start"]["line"]
        source = cast("str", block["source"])  # pragma: no mutate
        paragraph = source.rstrip("\r\n")
        if "\r" in paragraph or "\n" in paragraph:
            refusals.append(f"Line {line}: write each paragraph on one source line")
        visible = cast("str", block["visible"])  # pragma: no mutate
        if _RECORD_QUOTE.search(visible):
            refusals.append(f"Line {line}: start the line without a quoted sentence")


def _record_options(block: Json, content: list[Json], refusals: list[str]) -> None:
    metrics = cast("dict[str, int]", block["metrics"])  # pragma: no mutate
    if metrics["listItems"] > _RECORD_OPTION_LIMIT:
        refusals.append(f"{_RECORD_OPTIONS} exceeds {_RECORD_OPTION_LIMIT} bullets")
    if metrics["maxListDepth"] > 1 or any(
        group["kind"] != "list" or group["style"] != "unordered" for group in content
    ):
        refusals.append(f"{_RECORD_OPTIONS} must use flat bullet lists")
        return
    items = (
        item
        for group in content
        for item in cast("list[Json]", group["items"])  # pragma: no mutate
    )
    for index, item in enumerate(items, 1):
        if _record_words(item) > _RECORD_OPTION_WORD_LIMIT:
            refusals.append(f"Option {index} exceeds {_RECORD_OPTION_WORD_LIMIT} words")


def _record_structure(draft: Json, headings: tuple[object, ...]) -> list[str]:
    refusals: list[str] = []
    if headings not in _RECORD_SECTION_ORDERS:
        refusals.append(
            "Use these H2 sections in order: Context, Decision, "
            "optional Options considered, Consequences"
        )
    metrics = cast("dict[str, int]", draft["metrics"])  # pragma: no mutate
    if metrics["atxHeadings"] + metrics["setextHeadings"] != 1 + sum(
        heading is not None for heading in headings
    ):
        refusals.append("Use headings only for the H1 title and H2 sections")
    if metrics["tables"]:
        refusals.append("Remove tables")
    if metrics["fencedCodeBlocks"] or metrics["indentedCodeBlocks"]:
        refusals.append("Remove code blocks")
    if metrics["blockQuotes"]:
        refusals.append("Remove block quotes")
    if _record_words(draft) > _RECORD_TOTAL_LIMIT:
        refusals.append(f"Record body exceeds {_RECORD_TOTAL_LIMIT} words")
    return refusals


def check_decision_record(markdown: str) -> list[str]:
    """Return body refusals, or an empty list when the record passes."""
    try:
        # These fields describe the normalized draft. The check reads its body.
        draft = normalize(
            markdown,
            object="record",  # pragma: no mutate
            variant="decision-record",  # pragma: no mutate
            metadata={},  # pragma: no mutate
        )
    except ValueError as exc:
        return [str(exc)]
    blocks = cast("dict[str, Json]", draft["blocks"])  # pragma: no mutate
    order = cast("list[str]", draft["blockOrder"])  # pragma: no mutate
    sections = [blocks[key] for key in order[1:]]
    refusals = _record_structure(
        draft, tuple(block.get("heading") for block in sections)
    )
    title = blocks["title"]
    visible = cast("str", title["visible"])  # pragma: no mutate
    if _record_words(title) < _RECORD_TITLE_MINIMUM:
        refusals.append(f"Title needs at least {_RECORD_TITLE_MINIMUM} words")
    if visible.endswith((":", "?")):
        refusals.append("End the title without a colon or question mark")
    if _RECORD_QUOTE.search(visible):
        refusals.append("Start the title without a quoted sentence")
    for block in sections:
        heading = cast("str", block.get("heading", ""))  # pragma: no mutate
        limit = _RECORD_WORD_LIMITS.get(heading)
        if limit is not None and _record_words(block) > limit:
            refusals.append(f"{heading} exceeds {limit} words")
        content = cast("list[Json]", block["content"])  # pragma: no mutate
        if heading == _RECORD_OPTIONS:
            _record_options(block, content, refusals)
        _record_paragraphs(content, refusals)
    return refusals


def check_review(markdown: str) -> list[str]:
    """Return review validation refusals, or an empty list on success."""
    try:
        draft = normalize(markdown, object="pr-review", variant="review", metadata={})
    except ValueError as exc:
        return [str(exc)]
    try:
        cue = cue_runtime.resolve()
        with as_file(files("tbhbdev").joinpath("schemas")) as schemas:
            # Fixed schema names and pinned executable. Draft JSON goes on stdin.
            done = subprocess.run(  # nosec B603
                [
                    str(cue),
                    "vet",
                    "-c",
                    "-d",
                    "#Review",
                    "core.cue",
                    "issue.cue",
                    "review.cue",
                    "json:",
                    "-",
                ],
                cwd=schemas,
                input=json.dumps(draft),
                capture_output=True,
                encoding="utf-8",
                check=False,
                timeout=10,
            )
    except (cue_runtime.CueError, OSError, subprocess.TimeoutExpired) as exc:
        return [f"Review validation could not run: {exc}"]
    if done.returncode:
        return [done.stderr.strip() or "Review validation failed"]
    return []


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}/issues/{self.number}"


class Refused(RuntimeError):  # noqa: N818 -- Required public name.
    """A content refusal with a diagnostic."""


class Client(Protocol):
    def rest(self, method: str, path: str, payload: Json | None = None) -> Json: ...
    def graphql(self, query: str, variables: Json) -> Json: ...


class GhClient:
    """Use gh's environment credentials and JSON on stdin."""

    def rest(self, method: str, path: str, payload: Json | None = None) -> Json:
        argv = ["gh", "api", "--method", method, path]
        if payload is not None:
            argv += ["--input", "-"]
        # Fixed gh command and API arguments; content is JSON on stdin.
        done = subprocess.run(  # nosec B603
            argv, input=json.dumps(payload), capture_output=True, text=True, check=True
        )
        return cast("Json", json.loads(done.stdout))

    def graphql(self, query: str, variables: Json) -> Json:
        response = self.rest(
            "POST", "graphql", {"query": query, "variables": variables}
        )
        if response.get("errors"):
            message = "GitHub GraphQL write failed"
            raise RuntimeError(message)
        return response


def _scan(text: str, what: str) -> None:
    try:
        secret_scan.scan(
            text,
            what,
            root=root(),
            gitleaks=find_tool("gitleaks", unwrapped=True),
            visibility="",
        )
    except SystemExit as exc:
        raise Refused(str(exc)) from None


def _audit(
    ref: IssueRef, verb: str, scan: str = "not-needed", *, caller: str = "tbhbdev"
) -> None:
    path = Path(
        os.environ.get(
            "TBHBDEV_AUDIT_LOG",
            str(Path.home() / ".local/state/tbhb/content-audit.jsonl"),
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": datetime.now(UTC).isoformat(),
        "verb": verb,
        "caller": caller,
        "repo": f"{ref.owner}/{ref.repo}",
        "number": ref.number,
        "scan": scan,
    }
    with path.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


def scan_text(text: str, what: str) -> None:
    """Scan a record before the caller commits it."""
    _scan(text, what)


def render(template_path: Path, data: Mapping[str, str]) -> str:
    return re.sub(r"\{\{([^{}]+)\}\}", lambda m: data[m[1]], template_path.read_text())


def create_issue(  # noqa: PLR0913 -- Public plugin interface.
    repo: str,
    title: str,
    body: str,
    *,
    issue_type: str,
    labels: Sequence[str] = (),
    fields: Mapping[str, Json] | None = None,
    project: str | None = None,
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> IssueRef | Json:
    _scan(title, "issue title")
    _scan(body, "issue body")
    api = client if client is not None else GhClient()
    owner, name = repo.split("/")
    payload: Json = {
        "title": title,
        "body": body,
        "type": issue_type,
        "labels": list(labels),
    }
    if dry_run:
        if project is not None:
            return {
                "issue": payload,
                "project": _project_preview(project, "<created-issue-node-id>", fields),
            }
        return payload
    result = api.rest("POST", f"/repos/{repo}/issues", payload)
    ref = IssueRef(owner, name, cast("int", result["number"]))
    _audit(ref, "create_issue", "pass", caller=caller)
    if project is not None:
        add_to_project(ref, project, fields, client=api, caller=caller)
    return ref


def add_comment(
    ref: IssueRef,
    text: str,
    *,
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> Json | None:
    _scan(text, "issue comment")
    api = client if client is not None else GhClient()
    payload: Json = {"body": text}
    if dry_run:
        return payload
    api.rest("POST", f"{ref.path}/comments", payload)
    _audit(ref, "add_comment", "pass", caller=caller)
    return None


def close_issue(  # noqa: PLR0913 -- Public plugin interface.
    ref: IssueRef,
    reason: str,
    *,
    comment: str | None = None,
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> Json | None:
    state_reason = {
        "completed": "completed",
        "not-planned": "not_planned",
        "duplicate": "duplicate",
    }[reason]
    api = client if client is not None else GhClient()
    if reason == "completed":
        body = api.rest("GET", ref.path).get("body") or ""
        draft = normalize(f"# Issue\n\n{body}", object="issue", variant="", metadata={})
        if refusals := check_close(draft):
            message = "\n".join(format_refusal(refusal) for refusal in refusals)
            raise Refused(message)
    payload: Json = {"state": "closed", "state_reason": state_reason}
    if comment is not None:
        preview = add_comment(ref, comment, client=api, caller=caller, dry_run=dry_run)
        if dry_run:
            return {"comment": preview, "issue": payload}
    if dry_run:
        return payload
    api.rest("PATCH", ref.path, payload)
    _audit(ref, "close_issue", caller=caller)
    return None


def link_sub_issue(
    parent: IssueRef,
    child: IssueRef,
    *,
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> Json | None:
    api = client if client is not None else GhClient()
    issue = api.rest("GET", child.path)
    payload = {"sub_issue_id": issue["id"]}
    if dry_run:
        return payload
    api.rest("POST", f"{parent.path}/sub_issues", payload)
    _audit(parent, "link_sub_issue", caller=caller)
    return None


ADD_ITEM = """mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}"""
SET_FIELD = """mutation($project: ID!, $item: ID!, $field: ID!,
                       $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field, value: $value
  }) { projectV2Item { id } }
}"""


def _project_preview(
    project: str, content: object, fields: Mapping[str, Json] | None
) -> Json:
    return {
        "add_item": {
            "query": ADD_ITEM,
            "variables": {"project": project, "content": content},
        },
        "set_fields": [
            {
                "query": SET_FIELD,
                "variables": {
                    "project": project,
                    "item": "<created-project-item-id>",
                    "field": field,
                    "value": value,
                },
            }
            for field, value in (fields or {}).items()
        ],
    }


def add_to_project(  # noqa: PLR0913 -- Public plugin interface.
    ref: IssueRef,
    project: str,
    fields: Mapping[str, Json] | None = None,
    *,
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> Json | None:
    """Accept a project node ID and field IDs mapped to GraphQL field values."""
    api = client if client is not None else GhClient()
    issue = api.rest("GET", ref.path)
    variables = {"project": project, "content": issue["node_id"]}
    if dry_run:
        return _project_preview(project, issue["node_id"], fields)
    response = api.graphql(ADD_ITEM, variables)
    _audit(ref, "add_to_project", caller=caller)
    data = cast("Json", response["data"])
    added = cast("Json", data["addProjectV2ItemById"])
    item = cast("Json", added["item"])
    for field, value in (fields or {}).items():
        api.graphql(
            SET_FIELD,
            {
                "project": project,
                "item": item["id"],
                "field": field,
                "value": value,
            },
        )
        _audit(ref, "set_project_field", caller=caller)
    return None
