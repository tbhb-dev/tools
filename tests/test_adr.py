"""Exercise record drafting with a fake GitHub client."""

# cspell:words adrs

from datetime import date
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import Mock

import pytest

from tbhbdev import adr, cli
from tbhbdev.adr_decision import DECIDER_FIELD, REVERSIBILITY_FIELD, Decision
from tbhbdev.alpha.pull_request import parse_frontmatter
from tbhbdev.content import Json, Refused

PROG = "tbhbdev adr"
NEW = ["new", "--from-decision", "1"]
PULL = "https://github.com/example/records/pull/17"
QUESTION = "Where should we store notes?"
OPTION = "Shared folder - easy to search"
BODY = (
    f"See {PULL} for background.\n\n## Question\n\n{QUESTION}\n\n"
    f"## Options\n\n1. Separate folders - easy to sort\n2. {OPTION}\n\n"
    "## Recommendation\n\n2\n\n## Undo\n\nMove the notes\nback.\n"
)
PRIOR = "---\r\nstatus: 'stable' # Keep this.\r\ntags: [notes]\r\n"
END = "---\r\n\r\n# Prior record\r\n"
SLUG = "cafe-store-notes-together"


class Drafts:
    def __init__(self, root: Path, capture: pytest.CaptureFixture[str]) -> None:
        self.root, self.capture = root, capture
        self.repo = "example/records"
        self.issue: Json = {
            "type": {"name": "Decision"},
            "labels": [None, {"name": "other"}, {"name": "adr"}],
            "state": "open",
            "body": BODY,
            "title": "Café: Store notes together!",
            "adrLink": "not a pull request",
            "fields": {DECIDER_FIELD: "other", REVERSIBILITY_FIELD: "two-way"},
        }

    def rest(self, method: str, path: str, payload: Json | None = None) -> Json:
        assert (method, path, payload) == ("GET", f"/repos/{self.repo}/issues/1", None)
        return self.issue

    def graphql(self, query: str, variables: Json) -> Json:
        pytest.fail(f"unexpected GraphQL call: {query}, {variables}")

    def run(self, *args: str) -> int:
        argv = [*NEW, "--repo", self.repo, "--dir", str(self.root), *args]
        return adr.main(argv, PROG, client=self, clock=lambda: date(2026, 4, 3))

    def refuse(self, message: str, *args: str) -> None:
        assert self.run(*args) == 1
        assert self.capture.readouterr() == ("", f"{PROG}: {message}\n")


@pytest.fixture
def drafts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Drafts:
    unexpected = Mock(side_effect=AssertionError("unexpected real GitHub access"))
    monkeypatch.setattr(adr, "GhClient", unexpected)
    monkeypatch.setattr(adr, "origin_repository", unexpected)
    root = tmp_path / "records"
    root.mkdir()
    return Drafts(root, capsys)


@pytest.mark.parametrize(("old", "number"), [((), 1), ((2, 9998), 9999)])
def test_scaffold(drafts: Drafts, old: tuple[int, ...], number: int) -> None:
    for prefix in old:
        (drafts.root / f"{prefix:04d}-prior.md").touch()
    if not old:
        drafts.root = drafts.root / "nested" / "records"
    assert drafts.run() == 0
    path = drafts.root / f"{number:04d}-{SLUG}.md"
    assert drafts.capture.readouterr() == (f"{path}\n", "")
    text = path.read_text()
    title = f"{number:04d}. Café: Store notes together!"
    meta, body = parse_frontmatter(text)
    assert meta == {
        "type": "Decision Record",
        "title": title,
        "description": QUESTION,
        "tags": ["adrs", "decisions"],
        "status": "stable",
        "decided": "2026-04-03",
    }
    assert 'decided: "2026-04-03"\n' in text
    expected = f"\n# {title}\n\n## Context\n\n{QUESTION}\n\n## Decision\n\n{OPTION}\n\n"
    assert body == f"{expected}## Consequences\n\nMove the notes back.\n"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("type", None, "issue is not a Decision"),
        ("type", {"name": "Task"}, "issue is not a Decision"),
        ("labels", [{"name": "other"}], "Decision issue lacks the adr label"),
        ("labels", None, "Decision issue lacks the adr label"),
        ("state", "closed", "Decision issue is closed"),
        ("adrLink", PULL, "Decision issue already links to a record pull request"),
        (
            "body",
            BODY + f"\r\nadrLink: {PULL}\r\n",
            "Decision issue already links to a record pull request",
        ),
    ],
)
def test_issue_refusals(drafts: Drafts, key: str, value: object, message: str) -> None:
    drafts.issue[key] = value
    drafts.refuse(message)
    assert list(drafts.root.iterdir()) == []


@pytest.mark.parametrize(
    "text",
    [None, "", BODY.replace("## Question", "## Other"), BODY.replace("\n2\n", "\n3\n")],
)
def test_parse_refuses_missing_answer(drafts: Drafts, text: str | None) -> None:
    drafts.issue["body"] = text
    with pytest.raises(Refused, match=r"required text|numbered option"):
        Decision.parse(drafts.issue)


@pytest.mark.parametrize("prior", ["", "superseded_by: 6\r\n"])
def test_supersedes(drafts: Drafts, prior: str) -> None:
    old = drafts.root / "0007-prior.md"
    old.write_bytes((PRIOR + prior + END).encode())
    old.chmod(0o640)
    assert drafts.run("--supersedes", "0007") == 0
    new = drafts.root / f"0008-{SLUG}.md"
    context = f"{QUESTION}\n\nThis record supersedes [0007](0007-prior.md)."
    assert context in new.read_text()
    expected = PRIOR.replace("'stable'", "deprecated") + "superseded_by: 8\r\n" + END
    assert old.read_bytes() == expected.encode()
    assert old.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("prior", ["", "---\n[]\n---\n", "---\nstatus: |\n  x\n---\n"])
def test_refuses_invalid_prior(drafts: Drafts, prior: str) -> None:
    old = drafts.root / "0007-prior.md"
    old.write_text(prior)
    drafts.refuse(
        "expected block YAML frontmatter on the replaced record", "--supersedes", "7"
    )
    assert list(drafts.root.iterdir()) == [old]


def test_missing_prior_and_rollback(
    drafts: Drafts, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts.refuse("expected one record numbered 0007, found 0", "--supersedes", "7")
    old = drafts.root / "0007-prior.md"
    old.write_bytes((PRIOR + END).encode())
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("replace failed")))
    drafts.refuse("replace failed", "--supersedes", "7")
    assert list(drafts.root.iterdir()) == [old]
    assert old.read_bytes() == (PRIOR + END).encode()


def test_filename_refusals(drafts: Drafts) -> None:
    path = drafts.root / f"0001-{SLUG}.md"
    path.symlink_to("missing.md")
    drafts.refuse(f"record already exists: {path}")
    assert path.readlink() == Path("missing.md")
    (drafts.root / "9999-prior.md").touch()
    drafts.refuse("no four-digit decision record numbers remain")
    drafts.issue["title"] = "?!"
    drafts.refuse("issue title needs an ASCII letter or digit for the filename")


def test_cli_defaults_and_check_findings(
    drafts: Drafts, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(drafts.root)
    drafts.issue["title"] = "Search?"
    drafts.repo = "example/other"
    monkeypatch.setattr(adr, "origin_repository", lambda: drafts.repo)
    monkeypatch.setattr(adr, "GhClient", lambda: drafts)
    assert cli.TOOLS["adr"] is adr.main
    assert cli.main(["adr", *NEW]) == 0
    out, err = drafts.capture.readouterr()
    assert out == "adrs/0001-search.md\n"
    assert err.startswith("Draft needs editing. Title needs at least 4 words\n")


def test_read_errors(drafts: Drafts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adr, "origin_repository", Mock(side_effect=SystemExit("bad origin"))
    )
    assert adr.main(NEW, PROG, client=drafts) == 1
    assert "Supply --repo OWNER/REPO" in drafts.capture.readouterr().err
    monkeypatch.setattr(
        drafts, "rest", Mock(side_effect=CalledProcessError(1, ["read"]))
    )
    drafts.refuse("cannot read Decision issue from GitHub")


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["new"],
        ["new", "--from-decision", "0"],
        [*NEW, "--repo", "invalid"],
        [*NEW, "--supersedes", "-1"],
    ],
)
def test_argument_errors(drafts: Drafts, args: list[str]) -> None:
    assert adr.main(args, PROG, client=drafts) == 2
