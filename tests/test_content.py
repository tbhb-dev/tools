import json
import subprocess
from dataclasses import dataclass, field
lazy from collections.abc import Callable
lazy from pathlib import Path

import pytest

from tbhbdev import content as cw

type Call = tuple[str, str, cw.Json | None]
REF = cw.IssueRef("example", "demo", 7)
ITEM: cw.Json = {"data": {"addProjectV2ItemById": {"item": {"id": "ITEM"}}}}


@dataclass
class RecordingClient:
    calls: list[Call] = field(default_factory=list)

    def rest(self, method: str, path: str, payload: cw.Json | None = None) -> cw.Json:
        self.calls.append((method, path, payload))
        return {"number": 7, "id": 123, "node_id": "ISSUE"}

    def graphql(self, query: str, variables: cw.Json) -> cw.Json:
        self.calls.append(("graphql", query, variables))
        return ITEM


@pytest.fixture(autouse=True)
def scans(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def scan(
        text: str, what: str, *, root: Path, gitleaks: str, visibility: str
    ) -> None:
        assert (root, gitleaks, visibility) == (tmp_path, "gitleaks", "")
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
        assert set(entry) == {"time", "verb", "repo", "number", "scan"}
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
    client = RecordingClient()
    cw.close_issue(REF, reason, comment=comment, client=client)
    expected: list[Call] = []
    if comment is not None:
        expected.append(("POST", f"{REF.path}/comments", {"body": comment}))
    assert client.calls == [
        *expected,
        ("PATCH", REF.path, {"state": "closed", "state_reason": wire}),
    ]
    assert audit(tmp_path)[-1]["verb"] == "close_issue"


def test_link_sub_issue(tmp_path: Path) -> None:
    client = RecordingClient()
    child = cw.IssueRef("example", "other", 9)
    cw.link_sub_issue(REF, child, client=client)
    assert client.calls == [
        ("GET", child.path, None),
        ("POST", f"{REF.path}/sub_issues", {"sub_issue_id": 123}),
    ]
    assert audit(tmp_path)[0]["scan"] == "not-needed"


WRITES: list[Callable[[cw.Client], object]] = [
    lambda c: cw.create_issue(
        "example/demo", "refuse", "body", issue_type="Task", client=c
    ),
    lambda c: cw.create_issue(
        "example/demo", "title", "refuse", issue_type="Task", client=c
    ),
    lambda c: cw.add_comment(REF, "refuse", client=c),
    lambda c: cw.close_issue(REF, "completed", comment="refuse", client=c),
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
        cw._scan("refuse", "test")  # noqa: SLF001 -- Test the scan boundary.


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
    assert calls[0] == (
        ["gh", "api", "--method", "POST", "/repos/example/demo/issues", "--input", "-"],
        {"title": "Title", "body": "Body", "type": "Task", "labels": []},
    )
    assert calls[3] == (["gh", "api", "--method", "GET", REF.path], None)
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
