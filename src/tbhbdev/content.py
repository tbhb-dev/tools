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
    return _change_state(
        ref,
        {"state": "closed", "state_reason": state_reason},
        "close_issue",
        comment,
        client,
        caller,
        dry_run,
    )


def reopen_issue(
    ref: IssueRef,
    *,
    comment: str | None = None,
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> Json | None:
    return _change_state(
        ref,
        {"state": "open"},
        "reopen_issue",
        comment,
        client,
        caller,
        dry_run,
    )


def _change_state(  # noqa: PLR0913, PLR0917 -- Shared close/reopen implementation.
    ref: IssueRef,
    payload: Json,
    verb: str,
    comment: str | None,
    client: Client | None,
    caller: str,
    dry_run: bool,  # noqa: FBT001 -- Internal forwarding of public keyword.
) -> Json | None:
    api = client if client is not None else GhClient()
    if comment is not None:
        preview = add_comment(ref, comment, client=api, caller=caller, dry_run=dry_run)
        if dry_run:
            return {"comment": preview, "issue": payload}
    if dry_run:
        return payload
    api.rest("PATCH", ref.path, payload)
    _audit(ref, verb, caller=caller)
    return None


def _text_payload(title: str | None, body: str | None, what: str) -> Json:
    payload: Json = {}
    for key, text in (("title", title), ("body", body)):
        if text is not None:
            _scan(text, f"{what} {key}")
            payload[key] = text
    return payload


def edit_issue(  # noqa: PLR0913 -- Public plugin interface.
    ref: IssueRef,
    *,
    body: str | None = None,
    title: str | None = None,
    issue_type: str | None = None,
    fields: Mapping[str, Json] | None = None,  # noqa: ARG001 -- Project lookup deferred.
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> Json | None:
    """Edit REST fields; Project field updates await item lookup support."""
    payload = _text_payload(title, body, "issue")
    if issue_type is not None:
        payload["type"] = issue_type
    if dry_run:
        return payload
    api = client if client is not None else GhClient()
    api.rest("PATCH", ref.path, payload)
    _audit(
        ref,
        "edit_issue",
        "pass" if title is not None or body is not None else "not-needed",
        caller=caller,
    )
    return None


def write_pr_text(  # noqa: PLR0913 -- Public plugin interface.
    owner: str,
    repo: str,
    number: int,
    *,
    body: str | None = None,
    title: str | None = None,
    comment: str | None = None,
    client: Client | None = None,
    caller: str = "tbhbdev",
    dry_run: bool = False,
) -> Json | None:
    ref = IssueRef(owner, repo, number)
    payload = _text_payload(title, body, "pull request")
    if comment is not None:
        _scan(comment, "pull request comment")
    if dry_run:
        if comment is not None:
            return {"pull_request": payload, "comment": {"body": comment}}
        return payload
    api = client if client is not None else GhClient()
    if payload:
        api.rest("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", payload)
        _audit(ref, "write_pr_text", "pass", caller=caller)
    if comment is not None:
        api.rest("POST", f"{ref.path}/comments", {"body": comment})
        _audit(ref, "add_comment", "pass", caller=caller)
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
