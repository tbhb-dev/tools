"""Shared GitHub content writes for plugins; no templates or CLI of its own."""

import json
import os
import re
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from tbhbdev.alpha import secret_scan
from tbhbdev.alpha.pull_request import find_tool, root

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type Json = dict[str, object]


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}/issues/{self.number}"


class Refused(RuntimeError):  # noqa: N818 -- Required public name.
    """A scan refusal, with the scanner's redacted diagnostic."""


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


def _audit(ref: IssueRef, verb: str, scan: str = "not-needed") -> None:
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
        "repo": f"{ref.owner}/{ref.repo}",
        "number": ref.number,
        "scan": scan,
    }
    with path.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


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
) -> IssueRef:
    _scan(title, "issue title")
    _scan(body, "issue body")
    api = client if client is not None else GhClient()
    owner, name = repo.split("/")
    result = api.rest(
        "POST",
        f"/repos/{repo}/issues",
        {
            "title": title,
            "body": body,
            "type": issue_type,
            "labels": list(labels),
        },
    )
    ref = IssueRef(owner, name, cast("int", result["number"]))
    _audit(ref, "create_issue", "pass")
    if project is not None:
        add_to_project(ref, project, fields, client=api)
    return ref


def add_comment(ref: IssueRef, text: str, *, client: Client | None = None) -> None:
    _scan(text, "issue comment")
    api = client if client is not None else GhClient()
    api.rest("POST", f"{ref.path}/comments", {"body": text})
    _audit(ref, "add_comment", "pass")


def close_issue(
    ref: IssueRef,
    reason: str,
    *,
    comment: str | None = None,
    client: Client | None = None,
) -> None:
    state_reason = {
        "completed": "completed",
        "not-planned": "not_planned",
        "duplicate": "duplicate",
    }[reason]
    api = client if client is not None else GhClient()
    if comment is not None:
        add_comment(ref, comment, client=api)
    api.rest("PATCH", ref.path, {"state": "closed", "state_reason": state_reason})
    _audit(ref, "close_issue")


def link_sub_issue(
    parent: IssueRef,
    child: IssueRef,
    *,
    client: Client | None = None,
) -> None:
    api = client if client is not None else GhClient()
    issue = api.rest("GET", child.path)
    api.rest("POST", f"{parent.path}/sub_issues", {"sub_issue_id": issue["id"]})
    _audit(parent, "link_sub_issue")


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


def add_to_project(
    ref: IssueRef,
    project: str,
    fields: Mapping[str, Json] | None = None,
    *,
    client: Client | None = None,
) -> None:
    """Accept a project node ID and field IDs mapped to GraphQL field values."""
    api = client if client is not None else GhClient()
    issue = api.rest("GET", ref.path)
    response = api.graphql(ADD_ITEM, {"project": project, "content": issue["node_id"]})
    _audit(ref, "add_to_project")
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
        _audit(ref, "set_project_field")
