import json
import subprocess
from dataclasses import dataclass, field
from typing import Protocol
lazy from collections.abc import Callable
lazy from pathlib import Path

import pytest

from tbhbdev import content as cw
from tbhbdev.alpha import main as alpha_main
from tbhbdev.alpha import pull_request as pr

type Call = tuple[str, str, cw.Json | None]
REF = cw.IssueRef("example", "demo", 7)
CHILD = cw.IssueRef("example", "other", 9)
VISIBILITIES: list[str] = []
ITEM: cw.Json = {"data": {"addProjectV2ItemById": {"item": {"id": "ITEM"}}}}


@dataclass
class RecordingClient:
    calls: list[Call] = field(default_factory=list)
    body: str | None = None
    visibility: str = "private"
    full_name: str = "Example/Demo"
    reads: list[str] = field(default_factory=list)
    other_visibility: str = "public"
    issues: dict[str, cw.Json] = field(default_factory=dict)

    def rest(self, method: str, path: str, payload: cw.Json | None = None) -> cw.Json:
        if (method, path) == ("GET", "/repos/example/demo"):
            self.reads.append(path)
            return {"visibility": self.visibility, "full_name": self.full_name}
        if (method, path) == ("GET", "/repos/example/other"):
            self.reads.append(path)
            return {"visibility": self.other_visibility, "full_name": "example/other"}
        self.calls.append((method, path, payload))
        return {
            "number": 7,
            "id": 123,
            "node_id": "ISSUE",
            "body": self.body,
            **self.issues.get(path, {}),
        }

    def graphql(self, query: str, variables: cw.Json) -> cw.Json:
        self.calls.append(("graphql", query, variables))
        return ITEM


@pytest.fixture(autouse=True)
def scans(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    VISIBILITIES.clear()

    def scan(
        text: str, what: str, *, root: Path, gitleaks: str, visibility: str
    ) -> None:
        assert (root, gitleaks) == (tmp_path, "gitleaks")
        VISIBILITIES.append(visibility)
        seen.append((text, what))
        if text == "refuse":
            message = "rule test-rule on line 1"
            raise SystemExit(message)

    monkeypatch.setenv("TBHBDEV_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(cw, "root", lambda: tmp_path)

    def find_tool(name: str, **_kwargs: object) -> str:
        assert _kwargs == {"unwrapped": True}
        return name

    monkeypatch.setattr(cw, "find_tool", find_tool)
    monkeypatch.setattr(cw.secret_scan, "scan", scan)
    return seen


def audit(tmp_path: Path) -> list[cw.Json]:
    entries = [
        json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    for entry in entries:
        assert set(entry) == {"time", "verb", "repo", "number", "scan", "caller"}
        assert entry["repo"] == "example/demo"
        assert entry["number"] == 7
    return entries


def test_render(tmp_path: Path) -> None:
    template = tmp_path / "template.txt"
    template.write_text("{{name}}: {{name}}\n{{body}}")
    assert (
        cw.render(template, {"name": "Hello", "body": "{{literal}}"})
        == "Hello: Hello\n{{literal}}"
    )
    with pytest.raises(KeyError, match="body"):
        cw.render(template, {"name": "Hello"})


def test_create_issue(scans: list[tuple[str, str]], tmp_path: Path) -> None:
    client = RecordingClient()
    ref = cw.create_issue(
        "example/demo",
        "Title\n",
        "Body\n",
        issue_type="Task",
        labels=["pm"],
        client=client,
    )
    assert ref == REF
    assert client.calls == [
        (
            "POST",
            "/repos/example/demo/issues",
            {"title": "Title\n", "body": "Body\n", "type": "Task", "labels": ["pm"]},
        )
    ]
    assert scans == [("Title\n", "issue title"), ("Body\n", "issue body")]
    assert [(e["verb"], e["scan"]) for e in audit(tmp_path)] == [
        ("create_issue", "pass")
    ]


def test_project(tmp_path: Path) -> None:
    client = RecordingClient()
    cw.create_issue(
        "example/demo",
        "Title",
        "Body",
        issue_type="Task",
        project="PROJECT",
        fields={"FIELD": {"singleSelectOptionId": "OPTION"}},
        client=client,
    )
    assert client.calls[1:] == [
        ("GET", REF.path, None),
        ("graphql", cw.ADD_ITEM, {"project": "PROJECT", "content": "ISSUE"}),
        (
            "graphql",
            cw.SET_FIELD,
            {
                "project": "PROJECT",
                "item": "ITEM",
                "field": "FIELD",
                "value": {"singleSelectOptionId": "OPTION"},
            },
        ),
    ]
    assert [e["verb"] for e in audit(tmp_path)] == [
        "create_issue",
        "add_to_project",
        "set_project_field",
    ]
    cw.add_to_project(REF, "PROJECT", client=client)
    assert len(client.calls) == 6


def test_add_comment(scans: list[tuple[str, str]], tmp_path: Path) -> None:
    client = RecordingClient()
    cw.add_comment(REF, "Exact\ncomment", client=client)
    assert scans == [("Exact\ncomment", "issue comment")]
    assert client.calls == [
        ("POST", f"{REF.path}/comments", {"body": "Exact\ncomment"})
    ]
    assert audit(tmp_path)[0]["scan"] == "pass"


@pytest.mark.parametrize(
    ("reason", "wire"),
    [
        ("completed", "completed"),
        ("not-planned", "not_planned"),
        ("duplicate", "duplicate"),
    ],
)
@pytest.mark.parametrize("comment", [None, "Done\n"])
def test_close_issue(
    reason: str, wire: str, comment: str | None, tmp_path: Path
) -> None:
    box = "x" if reason == "completed" else " "
    client = RecordingClient(body=f"## Acceptance criteria\n- [{box}] Show preview")
    cw.close_issue(REF, reason, comment=comment, client=client)
    expected: list[Call] = [("GET", REF.path, None)] if reason == "completed" else []
    if comment is not None:
        expected.append(("POST", f"{REF.path}/comments", {"body": comment}))
    assert client.calls == [
        *expected,
        ("PATCH", REF.path, {"state": "closed", "state_reason": wire}),
    ]
    assert audit(tmp_path)[-1]["verb"] == "close_issue"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("body", "comment", "message"),
    [
        (
            "## Acceptance\n- [x] Open report\n## Acceptance\n- [ ] Show preview",
            "Done",
            r"blocks\.acceptance-2: Acceptance boxes are unchecked",
        ),
        ("## Verification\n- [ ] Show preview", "refuse", "rule test-rule on line 1"),
    ],
)
def test_close_refused(
    body: str, comment: str, message: str, dry_run: bool, tmp_path: Path
) -> None:
    client = RecordingClient(body=body)
    with pytest.raises(cw.Refused, match=message):
        cw.close_issue(
            REF, "completed", comment=comment, client=client, dry_run=dry_run
        )
    assert client.calls == [("GET", REF.path, None)]
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize(
    "case",
    [
        ("public", "private", "Initiative", "Epic"),
        ("private", "private", "Epic", "Feature"),
        ("private", "public", "Feature", "Task"),
        ("private", "public", "Feature", "Defect"),
        ("private", "public", "Feature", "Chore"),
        ("private", "public", "Feature", "Spike"),
        ("private", "public", "Feature", "Research"),
    ],
)
def test_link_sub_issue(
    case: tuple[str, str, str, str],
    tmp_path: Path,
    scans: list[tuple[str, str]],
) -> None:
    parent_vis, child_vis, parent_type, child_type = case
    client = RecordingClient(
        visibility=parent_vis,
        full_name="example/demo",
        other_visibility=child_vis,
        issues={
            REF.path: {"id": 456, "type": {"name": parent_type}},
            CHILD.path: {"type": {"name": child_type}},
        },
    )
    assert cw.link_sub_issue(REF, CHILD, client=client) is None
    assert client.calls == [
        ("GET", CHILD.path, None),
        ("GET", REF.path, None),
        ("POST", f"{REF.path}/sub_issues", {"sub_issue_id": 123}),
    ]
    assert client.reads == ["/repos/example/demo", "/repos/example/other"]
    (entry,) = audit(tmp_path)
    assert (entry["verb"], entry["scan"]) == ("link_sub_issue", "not-needed")
    assert entry["caller"] == "tbhbdev"
    assert scans == []


def test_link_sub_issue_reuses_same_repo_visibility(tmp_path: Path) -> None:
    child = cw.IssueRef("example", "demo", 9)
    client = RecordingClient(
        full_name="example/demo", issues={child.path: {"type": {"name": "Epic"}}}
    )
    assert cw.link_sub_issue(REF, child, client=client, dry_run=True) == {
        "sub_issue_id": 123
    }
    assert client.calls == [("GET", child.path, None), ("GET", REF.path, None)]
    assert client.reads == ["/repos/example/demo"]
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("parent", "child", "reason"),
    [
        (
            {"type": {"name": "Feature"}},
            {"type": {"name": child_type}},
            f"a private parent cannot have a public {child_type} child",
        )
        for child_type in ("Initiative", "Epic", "Feature")
    ]
    + [
        (
            {"type": {"name": "Feature"}},
            child,
            "a public child of a private parent must have a recognized issue type",
        )
        for child in (
            {"title": "Child"},
            {"type": {"name": "Other"}},
            {"type": None, "title": "Child"},
        )
    ]
    + [
        (
            parent,
            {"type": {"name": "Task"}},
            "only a private Feature can have a public leaf child",
        )
        for parent in (
            {"type": {"name": "Epic"}},
            {"title": "Parent"},
            {"type": "Feature"},
            {"type": {"id": 456}},
            {"type": {"name": ["Feature"]}},
        )
    ],
)
def test_link_sub_issue_refuses_visibility(
    parent: cw.Json, child: cw.Json, reason: str, dry_run: bool, tmp_path: Path
) -> None:
    client = RecordingClient(
        full_name="example/demo", issues={REF.path: parent, CHILD.path: child}
    )
    with pytest.raises(cw.Refused) as exc:
        cw.link_sub_issue(REF, CHILD, client=client, dry_run=dry_run)
    assert str(exc.value) == (
        f"Cannot link example/other#9 under example/demo#7: {reason}"
    )
    assert client.calls == [("GET", CHILD.path, None), ("GET", REF.path, None)]
    assert client.reads == ["/repos/example/demo", "/repos/example/other"]
    assert not (tmp_path / "audit.jsonl").exists()


class RelationshipWrite(Protocol):
    def __call__(
        self,
        first: cw.IssueRef,
        second: cw.IssueRef,
        /,
        *,
        client: cw.Client,
        caller: str = "tbhbdev",
        dry_run: bool = False,
    ) -> cw.Json | None: ...


@pytest.mark.parametrize(
    "case",
    [
        (
            cw.unlink_sub_issue,
            "unlink_sub_issue",
            "DELETE",
            "/sub_issue",
            "sub_issue_id",
            True,
        ),
        (
            cw.link_dependency,
            "link_dependency",
            "POST",
            "/dependencies/blocked_by",
            "issue_id",
            True,
        ),
        (
            cw.unlink_dependency,
            "unlink_dependency",
            "DELETE",
            "/dependencies/blocked_by/123",
            "issue_id",
            False,
        ),
    ],
)
def test_relationship_write(
    case: tuple[RelationshipWrite, str, str, str, str, bool],
    tmp_path: Path,
) -> None:
    write, verb, method, suffix, id_field, has_body = case
    client = RecordingClient()
    other = cw.IssueRef("example", "other", 9)
    payload: cw.Json = {id_field: 123}
    assert write(REF, other, client=client, caller="import", dry_run=True) == payload
    assert client.calls == [("GET", other.path, None)]
    assert not (tmp_path / "audit.jsonl").exists()

    assert write(REF, other, client=client) is None
    assert client.calls[1:] == [
        ("GET", other.path, None),
        (method, f"{REF.path}{suffix}", payload if has_body else None),
    ]
    (entry,) = audit(tmp_path)
    assert (entry["verb"], entry["caller"], entry["scan"]) == (
        verb,
        "tbhbdev",
        "not-needed",
    )


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        (
            ["link-sub", "example/demo#7", "example/other#9"],
            ("POST", "/sub_issues", {"sub_issue_id": 123}),
            "example/other",
            {"sub_issue_id": 123},
            0,
        ),
        (
            ["unlink-sub", "#7", "example/other#9"],
            ("DELETE", "/sub_issue", {"sub_issue_id": 123}),
            "example/other",
            {"sub_issue_id": 123},
            1,
        ),
        (
            ["block", "example/demo#7", "--by", "#9"],
            ("POST", "/dependencies/blocked_by", {"issue_id": 123}),
            "example/demo",
            {"issue_id": 123},
            1,
        ),
        (
            ["unblock", "example/demo#7", "--blocked-by", "example/other#9"],
            ("DELETE", "/dependencies/blocked_by/123", None),
            "example/other",
            {"issue_id": 123},
            0,
        ),
        (
            ["unblock", "#9", "--blocking", "#7"],
            ("DELETE", "/dependencies/blocked_by/123", None),
            "example/demo",
            {"issue_id": 123},
            1,
        ),
    ],
)
def test_alpha_issue_relationship(
    case: tuple[list[str], Call, str, cw.Json, int],
    dry_run: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    argv, write, lookup_repo, preview, origin_calls = case
    client = RecordingClient()
    lookups: list[str] = []

    def origin() -> str:
        lookups.append("example/demo")
        return "example/demo"

    def client_factory() -> RecordingClient:
        return client

    monkeypatch.setattr(pr, "origin_repository", origin)
    monkeypatch.setattr(cw, "GhClient", client_factory)
    cmd = ["issue", *argv]
    if dry_run:
        cmd.append("--dry-run")
    assert alpha_main(cmd, "tbhbdev alpha") == 0
    assert len(lookups) == origin_calls
    reads: list[Call] = [("GET", f"/repos/{lookup_repo}/issues/9", None)]
    if argv[0] == "link-sub":
        reads.append(("GET", REF.path, None))
        assert client.reads == ["/repos/example/demo", "/repos/example/other"]
    else:
        assert client.reads == []
    output = capsys.readouterr()
    assert output.err == ""
    if dry_run:
        assert json.loads(output.out) == preview
        assert client.calls == reads
        assert not (tmp_path / "audit.jsonl").exists()
    else:
        method, suffix, body = write
        assert client.calls == [*reads, (method, f"{REF.path}{suffix}", body)]
        assert output.out == ""
        (entry,) = audit(tmp_path)
        assert entry["caller"] == "tbhbdev alpha issue"


def test_alpha_issue_relationship_reports_api_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def fail(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv == ["gh", "api", "--method", "GET", "/repos/example/other/issues/9"]
        raise subprocess.CalledProcessError(4, argv, stderr="issue missing\n")

    monkeypatch.setattr(cw.subprocess, "run", fail)
    args = ["issue", "block", "example/demo#7", "--by", "example/other#9"]
    assert alpha_main(args, "tbhbdev alpha") == 4
    assert capsys.readouterr() == ("", "issue missing\n")
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("ref", ["7", "#0", "#-7", "#²", "bad#7", "example/..#7"])
def test_alpha_issue_relationship_rejects_bad_refs(
    ref: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        alpha_main(["issue", "link-sub", ref, "#9"], "tbhbdev alpha")
    assert exc.value.code == 2
    assert capsys.readouterr().err.endswith(": expected owner/repo#N or #N\n")


@pytest.mark.parametrize("direction", [(), ("--by", "#9", "--blocking", "#8")])
def test_alpha_issue_relationship_requires_one_direction(
    direction: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        alpha_main(["issue", "block", "#7", *direction], "tbhbdev alpha")
    assert exc.value.code == 2
    assert "--blocking" in capsys.readouterr().err


WRITES: list[Callable[[cw.Client], object]] = [
    lambda c: cw.create_issue(
        "example/demo", "refuse", "body", issue_type="Task", client=c
    ),
    lambda c: cw.create_issue(
        "example/demo", "title", "refuse", issue_type="Task", client=c
    ),
    lambda c: cw.add_comment(REF, "refuse", client=c),
]


@pytest.mark.parametrize("write", WRITES)
def test_refused_sends_nothing(
    write: Callable[[cw.Client], object], tmp_path: Path
) -> None:
    client = RecordingClient()
    with pytest.raises(cw.Refused, match=r"^rule test-rule on line 1$"):
        write(client)
    assert client.calls == []
    assert not (tmp_path / "audit.jsonl").exists()


def test_scan_refused() -> None:
    with pytest.raises(cw.Refused, match=r"^rule test-rule on line 1$"):
        cw.scan_text("refuse", "test")


def test_default_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], cw.Json | None]] = []

    def run(
        argv: list[str],
        *,
        input: str,  # noqa: A002 -- subprocess keyword.
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert (capture_output, text, check) == (True, True, True)
        payload = json.loads(input)
        calls.append((argv, payload))
        result = (
            ITEM
            if argv[4] == "graphql"
            else {"number": 7, "id": 123, "node_id": "ISSUE"}
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(result))

    monkeypatch.setattr(cw.subprocess, "run", run)
    cw.create_issue("example/demo", "Title", "Body", issue_type="Task")
    cw.add_comment(REF, "Comment")
    cw.close_issue(REF, "completed")
    cw.link_sub_issue(REF, REF)
    cw.add_to_project(REF, "PROJECT")
    assert calls[0] == (["gh", "api", "--method", "GET", "/repos/example/demo"], None)
    assert calls[1] == (
        ["gh", "api", "--method", "POST", "/repos/example/demo/issues", "--input", "-"],
        {"title": "Title", "body": "Body", "type": "Task", "labels": []},
    )
    assert (["gh", "api", "--method", "GET", REF.path], None) in calls
    assert calls[-1][1] == {
        "query": cw.ADD_ITEM,
        "variables": {"project": "PROJECT", "content": "ISSUE"},
    }
    monkeypatch.delenv("TBHBDEV_AUDIT_LOG")
    monkeypatch.setenv("HOME", str(tmp_path))
    cw.add_comment(REF, "Default audit")
    assert (tmp_path / ".local/state/tbhb/content-audit.jsonl").is_file()


def test_failed_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def failed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(cw.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        cw.close_issue(REF, "completed", comment="Accepted scan")
    assert not (tmp_path / "audit.jsonl").exists()

    def graph_error(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        result = (
            {"errors": [{"message": "failed"}]}
            if argv[4] == "graphql"
            else {"node_id": "ISSUE"}
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(result))

    monkeypatch.setattr(cw.subprocess, "run", graph_error)
    with pytest.raises(RuntimeError, match="GitHub GraphQL write failed"):
        cw.add_to_project(REF, "PROJECT")
    assert not (tmp_path / "audit.jsonl").exists()


def test_scan_text(scans: list[tuple[str, str]], tmp_path: Path) -> None:
    cw.scan_text("record", "plugin record")
    assert scans == [("record", "plugin record")]
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("caller", ["tbhbdev", "pm"])
def test_audit_caller(caller: str, tmp_path: Path) -> None:
    client = RecordingClient()
    cw.create_issue(
        "example/demo",
        "Title",
        "Body",
        issue_type="Task",
        project="PROJECT",
        fields={"FIELD": {"text": "value"}},
        client=client,
        caller=caller,
    )
    cw.add_comment(REF, "Comment", client=client, caller=caller)
    cw.close_issue(REF, "completed", comment="Done", client=client, caller=caller)
    cw.link_sub_issue(REF, REF, client=client, caller=caller)
    assert len(audit(tmp_path)) == 7
    assert {entry["caller"] for entry in audit(tmp_path)} == {caller}


@pytest.mark.parametrize("comment", [None, "Done"])
def test_dry_run_text(comment: str | None, tmp_path: Path) -> None:
    client = RecordingClient()
    assert cw.create_issue(
        "example/demo",
        "Title",
        "Body",
        issue_type="Task",
        client=client,
        dry_run=True,
    ) == {"title": "Title", "body": "Body", "type": "Task", "labels": []}
    assert cw.add_comment(REF, "Comment", client=client, dry_run=True) == {
        "body": "Comment"
    }
    payload: cw.Json = {"state": "closed", "state_reason": "completed"}
    expected = {"comment": {"body": comment}, "issue": payload} if comment else payload
    assert (
        cw.close_issue(
            REF,
            "completed",
            comment=comment,
            client=client,
            dry_run=True,
        )
        == expected
    )
    assert client.calls == [("GET", REF.path, None)]
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("fields", [None, {"FIELD": {"text": "value"}}])
def test_dry_run_ids(fields: dict[str, cw.Json] | None, tmp_path: Path) -> None:
    client = RecordingClient()
    project: cw.Json = {
        "add_item": {
            "query": cw.ADD_ITEM,
            "variables": {"project": "PROJECT", "content": "ISSUE"},
        },
        "set_fields": [
            {
                "query": cw.SET_FIELD,
                "variables": {
                    "project": "PROJECT",
                    "item": "<created-project-item-id>",
                    "field": "FIELD",
                    "value": {"text": "value"},
                },
            }
        ]
        if fields
        else [],
    }
    assert (
        cw.add_to_project(REF, "PROJECT", fields, client=client, dry_run=True)
        == project
    )
    created = cw.create_issue(
        "example/demo",
        "Title",
        "Body",
        issue_type="Task",
        project="PROJECT",
        fields=fields,
        client=client,
        dry_run=True,
    )
    project["add_item"] = {
        "query": cw.ADD_ITEM,
        "variables": {
            "project": "PROJECT",
            "content": "<created-issue-node-id>",
        },
    }
    assert created == {
        "issue": {"title": "Title", "body": "Body", "type": "Task", "labels": []},
        "project": project,
    }
    assert cw.link_sub_issue(REF, REF, client=client, dry_run=True) == {
        "sub_issue_id": 123
    }
    assert client.calls == [("GET", REF.path, None)] * 3
    assert not (tmp_path / "audit.jsonl").exists()


DRY_WRITES: list[Callable[[cw.Client, bool], object]] = [
    lambda c, dry: cw.create_issue(
        "example/demo", "Title", "refuse", issue_type="Task", client=c, dry_run=dry
    ),
    lambda c, dry: cw.add_comment(REF, "refuse", client=c, dry_run=dry),
]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("write", DRY_WRITES)
def test_dry_run_refused(
    write: Callable[[cw.Client, bool], object],
    dry_run: bool,
    tmp_path: Path,
) -> None:
    client = RecordingClient()
    with pytest.raises(cw.Refused):
        write(client, dry_run)
    assert client.calls == []
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize(
    ("reported", "full_name", "passed"),
    [
        ("private", "example/demo", "PRIVATE"),
        ("private", "Example/Demo", ""),
        ("private", "example/dem\u0131", ""),
        ("private", "example/other", ""),
        ("private", "", ""),
        ("PRIVATE", "example/demo", ""),
        ("private".replace("i", "\u0131"), "example/demo", ""),
        ("public", "example/demo", ""),
        ("internal", "example/demo", ""),
        ("", "example/demo", ""),
    ],
)
def test_writes_scan_with_the_repository_visibility(
    reported: str, full_name: str, passed: str
) -> None:
    client = RecordingClient(visibility=reported, full_name=full_name)
    cw.create_issue("example/demo", "Title", "Body", issue_type="Task", client=client)
    cw.add_comment(REF, "Comment", client=client)
    assert client.reads == ["/repos/example/demo"] * 2
    assert [passed] * 3 == VISIBILITIES
