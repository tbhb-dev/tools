# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for tbhbdev alpha pr close and alpha issue close, with a stubbed gh."""

import json
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import close, opened
from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    from pathlib import Path

ENV = {"CLAUDE_CODE_SESSION_ID": "raw-session"}
URL = "https://github.com/tbhb-dev/ascm/pull/7"
OPENED_LINE = "Opened-by: LLM harness=claude-code;model=anthropic:m1"
POSTING = (
    "LLM harness=claude-code:2.1.0;model=anthropic:m1;"
    "session=sha-256-64:aaaaaaaaaaaaaaaa"
)
BODY = f"Why.\n\n{OPENED_LINE}\n"


@dataclass
class Gh:
    """A stub gh: canned view output, and a record of what ran."""

    view: dict[str, object] = field(
        default_factory=lambda: {"state": "OPEN", "url": URL, "body": BODY}
    )
    view_rc: int = 0
    close_rc: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[1] == "view":
            out = json.dumps(self.view) if self.view_rc == 0 else ""
            return subprocess.CompletedProcess(argv, self.view_rc, out, "no view")
        return subprocess.CompletedProcess(argv, self.close_rc, "closed\n", "warn\n")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Gh:
    stub = Gh()
    monkeypatch.setattr(close, "gh", stub)
    monkeypatch.setattr(pr, "posting_value", lambda env: POSTING if env else None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return stub


def root() -> str:
    ident = opened.opener(ENV)
    assert ident is not None
    return ident.root


def trailer_line() -> str:
    value = pr.posting_value(ENV)
    assert value is not None
    return f"{agent_identity.KEY_GEN}: {value}"


def test_close_pr_it_opened_comments_the_reason_and_the_trailer(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    opened.record_opened(root(), ["tbhb-dev/ascm#7"])
    assert close.main(["pr", "close", "7", "--reason", "superseded"], env=ENV) == 0
    assert gh.calls[-1] == [
        "pr",
        "close",
        "7",
        "--comment",
        f"superseded\n\n{trailer_line()}\n",
    ]
    out = capsys.readouterr()
    assert (out.out, out.err) == ("closed\n", "warn\n")


def test_close_another_sessions_pr_records_that_in_the_comment(gh: Gh) -> None:
    assert close.main(["pr", "close", "7", "--reason", "stale"], env=ENV) == 0
    assert gh.calls[-1][-1] == (
        f"Closed by a session other than the opener.\n\nstale\n\n{trailer_line()}\n"
    )


def test_a_pr_without_opened_by_is_refused(gh: Gh) -> None:
    gh.view["body"] = "Why.\n"
    with pytest.raises(SystemExit, match="no Opened-by line"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)
    assert all(call[1] == "view" for call in gh.calls)


def test_a_pr_that_is_not_open_is_refused(gh: Gh) -> None:
    gh.view["state"] = "MERGED"
    with pytest.raises(SystemExit, match="pr 7: it is MERGED, not OPEN"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


def test_an_unreadable_pr_is_refused(gh: Gh) -> None:
    gh.view_rc = 1
    with pytest.raises(SystemExit, match="cannot read pr 7: no view"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


def test_a_caller_that_is_not_an_agent_is_refused(gh: Gh) -> None:
    with pytest.raises(SystemExit, match="isn't a recognised agent session"):
        close.main(["pr", "close", "7", "--reason", "x"], env={})
    assert gh.calls == []


@pytest.mark.usefixtures("gh")
def test_an_invalid_trailer_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr, "posting_value", lambda _env: "not llm")
    with pytest.raises(SystemExit, match="invalid LLM trailer: not llm: "):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


@pytest.mark.usefixtures("gh")
def test_a_corrupt_ledger_is_reported() -> None:
    path = opened.ledger_path(root())
    path.parent.mkdir(parents=True)
    path.write_text("nope")
    with pytest.raises(SystemExit, match="is not JSON"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


def test_a_pr_url_that_is_not_a_pr_is_refused(gh: Gh) -> None:
    gh.view["url"] = "https://github.com/tbhb-dev/ascm/issues/7"
    with pytest.raises(SystemExit, match="cannot read a pull request from"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


def test_dry_run_prints_the_command_and_closes_nothing(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    opened.record_opened(root(), ["tbhb-dev/ascm#7"])
    assert close.main(["pr", "close", "7", "--reason", "r", "--dry-run"], env=ENV) == 0
    assert [call[1] for call in gh.calls] == ["view"]
    assert capsys.readouterr().out.startswith("gh pr close 7 --comment ")


def test_a_failing_close_returns_gh_exit_code(gh: Gh) -> None:
    opened.record_opened(root(), ["tbhb-dev/ascm#7"])
    gh.close_rc = 4
    assert close.main(["pr", "close", "7", "--reason", "r"], env=ENV) == 4


def test_close_issue_with_a_reason_and_a_comment(gh: Gh) -> None:
    gh.view = {"state": "OPEN", "url": "https://github.com/tbhb-dev/ascm/issues/3"}
    args = [
        "issue",
        "close",
        "3",
        "--reason",
        "not-planned",
        "--comment",
        "Out of scope.",
    ]
    assert close.main(args, env=ENV) == 0
    assert gh.calls[-1] == [
        "issue",
        "close",
        "3",
        "--reason",
        "not planned",
        "--comment",
        f"Out of scope.\n\n{trailer_line()}\n",
    ]


def test_close_issue_without_a_comment_uses_a_default(gh: Gh) -> None:
    gh.view = {"state": "OPEN", "url": "x"}
    assert close.main(["issue", "close", "3", "--reason", "completed"], env=ENV) == 0
    assert gh.calls[-1][-1] == f"Closed.\n\n{trailer_line()}\n"
    assert gh.calls[-1][4] == "completed"


def test_a_closed_issue_is_refused(gh: Gh) -> None:
    gh.view = {"state": "CLOSED", "url": "x"}
    with pytest.raises(SystemExit, match="issue 3: it is CLOSED, not OPEN"):
        close.main(["issue", "close", "3", "--reason", "completed"], env=ENV)


def test_an_unknown_issue_reason_is_a_usage_error(gh: Gh) -> None:
    with pytest.raises(SystemExit) as exc:
        close.main(["issue", "close", "3", "--reason", "whatever"], env=ENV)
    assert exc.value.code == 2
    assert gh.calls == []


def test_the_real_gh_runs_through_pr_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pr, "find_tool", lambda name: f"/bin/{name}")
    monkeypatch.setattr(pr, "run", fake_run)
    assert close.gh(["pr", "view"]).returncode == 0
    assert seen == [["/bin/gh", "pr", "view"]]
