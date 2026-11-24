"""Decision parsing, local record drafts, and command dispatch."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from subprocess import CalledProcessError
from typing import TYPE_CHECKING

import pytest

from tbhbdev import adr, cli
from tbhbdev.adr_decision import DECIDER_FIELD, REVERSIBILITY_FIELD, Decision
from tbhbdev.alpha.pull_request import parse_frontmatter
from tbhbdev.content import Refused

if TYPE_CHECKING:
    from tbhbdev.content import Json

PROG = "tbhbdev adr"
REPO = "example/records"
PULL = "https://github.com/example/records/pull/23"
ARGS = ["new", "--from-decision", "42"]
TITLE = "Café: Store notes together!"
SLUG = "cafe-store-notes-together"
QUESTION = "Where should we store notes?"
CHOICE = "Shared folder - easy to search"
UNDO = "Move the notes back."
BODY = f"""## Question

Where should we
store notes?

## Options

1. Separate folders - easier to organize
2. {CHOICE}

## Recommendation

2

## Undo

Move the notes
back.
"""
PRIOR_RECORD = """---
type: Decision Record
title: 0007. Keep notes in separate folders
description: >-
  Keep notes in separate folders.
tags: [decisions] # Keep this comment.
status: 'stable' # Keep this one too.
decided: "2026-01-01"
---

# 0007. Keep notes in separate folders

## Context

Notes belong to different projects.
"""


class FakeClient:
    def __init__(self, issue: Json) -> None:
        self.issue = issue
        self.calls: list[tuple[str, str, Json | None]] = []

    def rest(self, method: str, path: str, payload: Json | None = None) -> Json:
        self.calls.append((method, path, payload))
        return self.issue

    def graphql(self, query: str, variables: Json) -> Json:
        message = f"unexpected GraphQL call: {query} {variables}"
        raise AssertionError(message)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient(
        {
            "type": {"name": "Decision"},
            "labels": [None, {"name": "other"}, {"name": "adr"}],
            "state": "open",
            "title": TITLE,
            "body": BODY,
        }
    )


def _today() -> date:
    return date(2026, 4, 3)


def _run(directory: Path, client: FakeClient, *extra: str) -> int:
    return adr.main(
        [*ARGS, "--repo", REPO, "--dir", str(directory), *extra],
        PROG,
        client=client,
        clock=_today,
    )


def _files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file())


@pytest.mark.parametrize(
    ("existing", "expected"), [((), "0001"), (("0002", "0010"), "0011")]
)
def test_new_uses_highest_prefix_and_prints_path(
    tmp_path: Path,
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
    existing: tuple[str, ...],
    expected: str,
) -> None:
    for prefix in existing:
        (tmp_path / f"{prefix}-prior-record.md").touch()
    (tmp_path / "9988-not-a-record.txt").touch()
    (tmp_path / "9987-not-a-file.md").mkdir()
    (tmp_path / "README.md").write_text("Index text\n", encoding="utf-8")
    assert _run(tmp_path, client) == 0
    assert client.calls == [("GET", "/repos/example/records/issues/42", None)]
    path = tmp_path / f"{expected}-{SLUG}.md"
    assert capsys.readouterr() == (f"{path}\n", "")
    text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    assert metadata == {
        "type": "Decision Record",
        "title": f"{expected}. {TITLE}",
        "description": QUESTION,
        "tags": ["adrs", "decisions"],
        "status": "stable",
        "decided": "2026-04-03",
    }
    assert 'decided: "2026-04-03"\n' in text
    assert body == (
        f"\n# {expected}. {TITLE}\n\n## Context\n\n{QUESTION}\n\n"
        f"## Decision\n\n{CHOICE}\n\n## Consequences\n\n{UNDO}\n"
    )
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "Index text\n"


def _refuses(
    directory: Path,
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    assert _run(directory, client) == 1
    assert capsys.readouterr() == ("", f"{PROG}: {message}\n")
    assert not directory.exists()


def test_refuses_issue_without_record_label(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.issue["labels"] = [{"name": "other"}]
    _refuses(tmp_path / "adrs", client, capsys, "Decision issue lacks the adr label")


def test_refuses_closed_issue(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.issue["state"] = "closed"
    _refuses(tmp_path / "adrs", client, capsys, "Decision issue is closed")


@pytest.mark.parametrize(
    "linked", [{"adrLink": PULL}, {"body": f"{BODY}\r\nadrLink: {PULL}\r\n"}]
)
def test_refuses_issue_with_record_pull_request(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str], linked: Json
) -> None:
    client.issue.update(linked)
    _refuses(
        tmp_path / "adrs",
        client,
        capsys,
        "Decision issue already has an ADR pull request link",
    )


@pytest.mark.parametrize("kind", [None, {"name": "Task"}])
def test_refuses_other_issue_type(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str], kind: object
) -> None:
    client.issue["type"] = kind
    _refuses(tmp_path / "adrs", client, capsys, "issue is not a Decision")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"body": None}, "Decision issue is missing required text: body"),
        ({"title": " \t "}, "Decision issue is missing required text: title"),
        (
            {"body": BODY.replace("## Question", "## Background")},
            "Decision issue is missing required text: Question",
        ),
        (
            {"body": BODY.replace(UNDO.split(maxsplit=1)[0] + " the notes\nback.", "")},
            "Decision issue is missing required text: Undo",
        ),
        (
            {"body": BODY.replace("\n2\n", "\n3\n")},
            "Recommendation must name a numbered option",
        ),
        ({"labels": None}, "Decision issue lacks the adr label"),
    ],
)
def test_invalid_decision_metadata_fails_loudly(
    client: FakeClient, change: Json, message: str
) -> None:
    client.issue.update(change)
    with pytest.raises(Refused) as error:
        Decision.parse(client.issue)
    assert str(error.value) == message


def test_parse_ignores_unrelated_links_and_organization_fields(
    client: FakeClient,
) -> None:
    client.issue["adrLink"] = "https://github.com/example/records/issues/23"
    client.issue["body"] = f"See {PULL} for background.\n\n{BODY}".replace("\n", "\r\n")
    client.issue["fields"] = {DECIDER_FIELD: "other", REVERSIBILITY_FIELD: "two-way"}
    assert Decision.parse(client.issue) == Decision(TITLE, QUESTION, CHOICE, UNDO)


def test_supersedes_changes_only_two_keys_and_keeps_line_endings(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / "0007-keep-notes-in-separate-folders.md"
    old.write_text(PRIOR_RECORD.replace("\n", "\r\n"), encoding="utf-8", newline="")
    old.chmod(0o640)
    assert _run(tmp_path, client, "--supersedes", "0007") == 0
    expected = PRIOR_RECORD.replace("'stable'", "deprecated").replace(
        'decided: "2026-01-01"\n', 'decided: "2026-01-01"\nsuperseded_by: 8\n'
    )
    assert old.read_bytes() == expected.replace("\n", "\r\n").encode()
    assert old.stat().st_mode & 0o777 == 0o640
    new = tmp_path / f"0008-{SLUG}.md"
    meta, body = parse_frontmatter(new.read_text(encoding="utf-8"))
    assert isinstance(meta, dict)
    assert meta["status"] == "stable"
    assert "superseded_by" not in meta
    assert body == (
        f"\n# 0008. {TITLE}\n\n## Context\n\n{QUESTION}\n\n"
        f"This record supersedes [0007]({old.name}).\n\n"
        f"## Decision\n\n{CHOICE}\n\n## Consequences\n\n{UNDO}\n"
    )
    assert capsys.readouterr() == (f"{new}\n", "")
    assert _files(tmp_path) == [old, new]


def test_supersedes_updates_existing_integer_in_place(
    tmp_path: Path, client: FakeClient
) -> None:
    old = tmp_path / "0007-prior.md"
    text = PRIOR_RECORD.replace(
        "status: 'stable'", "superseded_by: 4\nstatus: deprecated"
    )
    old.write_text(text, encoding="utf-8")
    assert _run(tmp_path, client, "--supersedes", "7") == 0
    assert old.read_text(encoding="utf-8") == text.replace(
        "superseded_by: 4", "superseded_by: 8"
    )


@pytest.mark.parametrize("names", [[], ["0007-first.md", "0007-second.md"]])
def test_refuses_missing_or_ambiguous_replaced_record(
    tmp_path: Path,
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
    names: list[str],
) -> None:
    for name in names:
        (tmp_path / name).write_text(PRIOR_RECORD, encoding="utf-8")
    assert _run(tmp_path, client, "--supersedes", "0007") == 1
    assert capsys.readouterr() == (
        "",
        f"{PROG}: expected one record numbered 0007, found {len(names)}\n",
    )
    assert [path.name for path in _files(tmp_path)] == names


@pytest.mark.parametrize(
    "source",
    [
        "no frontmatter",
        "---\n[\n---\n",
        "---\n[stable]\n---\n",
        "---\n{status: stable}\n---\n",
        "---\nstatus: [stable]\n---\n",
        "---\nstatus: |\n  stable\n---\n",
        "---\nstatus:\n---\n",
    ],
)
def test_refuses_unsafe_replacement_frontmatter(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    old = tmp_path / "0007-prior.md"
    old.write_text(source, encoding="utf-8")
    assert _run(tmp_path, client, "--supersedes", "7") == 1
    assert capsys.readouterr() == (
        "",
        f"{PROG}: expected block YAML frontmatter in {old}\n",
    )
    assert old.read_text(encoding="utf-8") == source
    assert _files(tmp_path) == [old]


def test_supersedes_rolls_back_when_replacing_fails(
    tmp_path: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = tmp_path / "0007-prior.md"
    old.write_text(PRIOR_RECORD, encoding="utf-8")

    def fail(_self: Path, target: Path) -> Path:
        assert target == old
        message = "replace failed"
        raise OSError(message)

    monkeypatch.setattr(Path, "replace", fail)
    assert _run(tmp_path, client, "--supersedes", "7") == 1
    assert capsys.readouterr() == (
        "",
        f"{PROG}: cannot write decision record: replace failed\n",
    )
    assert old.read_text(encoding="utf-8") == PRIOR_RECORD
    assert _files(tmp_path) == [old]


def test_refuses_existing_path_without_touching_it(
    tmp_path: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / f"0001-{SLUG}.md"

    def arrive(_body: str) -> list[str]:
        path.write_text("Existing record\n", encoding="utf-8")
        return []

    monkeypatch.setattr(adr, "check_decision_record", arrive)
    assert _run(tmp_path, client) == 1
    assert capsys.readouterr() == ("", f"{PROG}: record already exists: {path}\n")
    assert path.read_text(encoding="utf-8") == "Existing record\n"


def test_refuses_number_overflow(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    last = tmp_path / "9999-last.md"
    last.touch()
    assert _run(tmp_path, client) == 1
    assert capsys.readouterr() == (
        "",
        f"{PROG}: no four-digit decision record numbers remain\n",
    )
    assert _files(tmp_path) == [last]


def test_refuses_title_without_usable_slug(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.issue["title"] = "?!"
    _refuses(
        tmp_path / "adrs",
        client,
        capsys,
        "issue title needs a letter or digit for the record filename",
    )


def test_body_check_findings_are_advisory(
    tmp_path: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.issue["title"] = "Search?"
    path = tmp_path / "0001-search.md"
    assert _run(tmp_path, client) == 0
    findings = (
        f"{PROG}: draft needs editing. Title needs at least 4 words\n"
        f"{PROG}: draft needs editing. End the title without a colon or question mark\n"
    )
    assert capsys.readouterr() == (f"{path}\n", findings)
    assert path.is_file()


def test_cli_dispatch_uses_default_client_origin_and_directory(
    tmp_path: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adr, "GhClient", lambda: client)
    monkeypatch.setattr(adr, "origin_repository", lambda: "example/other")
    assert cli.TOOLS["adr"] is adr.main
    assert cli.main(["adr", *ARGS]) == 0
    path = Path("adrs") / f"0001-{SLUG}.md"
    assert capsys.readouterr() == (f"{path}\n", "")
    assert path.is_file()
    today = date.today()  # noqa: DTZ011 -- The command uses the caller's local date.
    assert f'decided: "{today.isoformat()}"' in path.read_text(encoding="utf-8")
    assert client.calls == [("GET", "/repos/example/other/issues/42", None)]


def test_refuses_when_origin_cannot_be_resolved(
    tmp_path: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing() -> str:
        message = "cannot read origin"
        raise SystemExit(message)

    monkeypatch.setattr(adr, "origin_repository", missing)
    directory = tmp_path / "adrs"
    assert adr.main([*ARGS, "--dir", str(directory)], PROG, client=client) == 1
    assert capsys.readouterr() == (
        "",
        f"{PROG}: cannot read origin. Supply --repo OWNER/REPO\n",
    )
    assert not client.calls
    assert not directory.exists()


def test_refuses_failed_read(
    tmp_path: Path,
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(method: str, path: str) -> Json:
        raise CalledProcessError(1, [method, path])

    monkeypatch.setattr(client, "rest", fail)
    _refuses(
        tmp_path / "adrs",
        client,
        capsys,
        "cannot read issue example/records#42 from GitHub",
    )


@pytest.mark.parametrize(
    ("args", "diagnostic"),
    [
        ([], "the following arguments are required: command"),
        (["new"], "the following arguments are required: --from-decision"),
        (["new", "--from-decision", "0"], "expected a positive number"),
        (["new", "--from-decision", "text"], "argument --from-decision"),
        ([*ARGS, "--repo", "single"], "expected OWNER/REPO"),
        ([*ARGS, "--supersedes", "-7"], "expected a positive number"),
        ([*ARGS, "--dir"], "argument --dir: expected one argument"),
    ],
)
def test_cli_argument_errors(
    args: list[str],
    diagnostic: str,
    client: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert adr.main(args, PROG, client=client) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert diagnostic in captured.err
    assert not client.calls


def test_help_lists_record_options(capsys: pytest.CaptureFixture[str]) -> None:
    assert adr.main(["--help"], PROG) == 0
    assert "Draft a decision record from an issue." in capsys.readouterr().out
    assert adr.main(["new", "--help"], PROG) == 0
    help_text = capsys.readouterr().out
    for argument in (
        "--from-decision N",
        "--repo OWNER/REPO",
        "--dir DIR",
        "--supersedes NNNN",
    ):
        assert argument in help_text
