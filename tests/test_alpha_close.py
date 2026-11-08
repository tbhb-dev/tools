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
    from collections.abc import Mapping
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
        default_factory=lambda: {
            "state": "OPEN",
            "url": URL,
            "body": BODY,
            "headRefName": "feat/x y",
            "isCrossRepository": False,
        }
    )
    view_rc: int = 0
    close_rc: int = 0
    delete_rc: int = 0
    delete_stderr: str = "denied\n"
    list_out: str = "[]"
    list_rc: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[1] == "view":
            out = json.dumps(self.view) if self.view_rc == 0 else ""
            return subprocess.CompletedProcess(argv, self.view_rc, out, "no view")
        if argv[1] == "list":
            return subprocess.CompletedProcess(
                argv, self.list_rc, self.list_out, "no list"
            )
        if argv[0] == "api":
            return subprocess.CompletedProcess(
                argv, self.delete_rc, "", self.delete_stderr
            )
        return subprocess.CompletedProcess(argv, self.close_rc, "closed\n", "warn\n")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Gh:
    stub = Gh()
    monkeypatch.setattr(close, "gh", stub)

    def posting_value(env: Mapping[str, str]) -> str | None:
        return POSTING if env else None

    monkeypatch.setattr(pr, "posting_value", posting_value)
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
    with pytest.raises(SystemExit, match="isn't a recognized agent session"):
        close.main(["pr", "close", "7", "--reason", "x"], env={})
    assert gh.calls == []


@pytest.mark.usefixtures("gh")
def test_an_invalid_trailer_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def posting_value(_env: Mapping[str, str]) -> str | None:
        return "not llm"

    monkeypatch.setattr(pr, "posting_value", posting_value)
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


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
@pytest.mark.parametrize(
    "argv",
    [
        ["pr", "close", "7", "--reason"],
        ["issue", "close", "3", "--reason", "completed", "--comment"],
    ],
)
def test_a_blank_reason_or_comment_is_a_usage_error(
    gh: Gh, capsys: pytest.CaptureFixture[str], argv: list[str], blank: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        close.main([*argv, blank], env=ENV)
    assert exc.value.code == 2
    assert "must not be blank" in capsys.readouterr().err
    assert gh.calls == []


def test_a_non_blank_reason_is_kept_as_given(gh: Gh) -> None:
    assert close.main(["pr", "close", "7", "--reason", "  why  "], env=ENV) == 0
    assert "  why  \n" in gh.calls[-1][-1]


def test_the_real_gh_runs_through_pr_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def find_tool(name: str) -> str:
        return f"/bin/{name}"

    monkeypatch.setattr(pr, "find_tool", find_tool)
    monkeypatch.setattr(pr, "run", fake_run)
    assert close.gh(["pr", "view"]).returncode == 0
    assert seen == [["/bin/gh", "pr", "view"]]


def test_the_parser_describes_itself_and_names_its_prog(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = close.build_parser("p")
    assert parser.prog == "p"
    assert parser.description == "Close a PR or issue."
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    assert "Close a PR or issue." in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_the_parser_requires_a_reason(
    kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        close.build_parser("p").parse_args([kind, "close", "3"])
    assert exc.value.code == 2
    assert "--reason" in capsys.readouterr().err


def test_the_parser_requires_a_kind_and_a_verb() -> None:
    cases: tuple[list[str], ...] = ([], ["pr"], ["issue"])
    for argv in cases:
        with pytest.raises(SystemExit):
            close.build_parser("p").parse_args(argv)


def test_the_parser_reads_the_pr_options() -> None:
    args = close.build_parser("p").parse_args(
        ["pr", "close", "3", "--reason", "r", "--dry-run"]
    )
    assert (args.kind, args.verb, args.number, args.reason, args.dry_run) == (
        "pr",
        "close",
        3,
        "r",
        True,
    )


def test_the_parser_reads_the_issue_options() -> None:
    args = close.build_parser("p").parse_args(
        ["issue", "close", "3", "--reason", "completed", "--comment", "c"]
    )
    assert (args.kind, args.reason, args.comment, args.dry_run) == (
        "issue",
        "completed",
        "c",
        False,
    )


def test_main_names_the_prog_in_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        close.main(["--help"], prog="prog", env=ENV)
    assert capsys.readouterr().out.startswith("usage: prog ")


def test_main_reads_the_process_environment_by_default(
    gh: Gh, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh.view = {"state": "OPEN", "url": "x"}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "raw-session")
    argv = ["issue", "close", "3", "--reason", "completed", "--dry-run"]
    assert close.main(argv) == 0
    assert capsys.readouterr().out.startswith("gh issue close 3 ")


def test_view_asks_gh_for_the_state_url_and_body(gh: Gh) -> None:
    close.view("issue", 7)
    assert gh.calls == [["issue", "view", "7", "--json", "state,url,body"]]


def test_view_of_a_pr_also_asks_for_the_head_branch(gh: Gh) -> None:
    close.view("pr", 7)
    fields = "state,url,body,headRefName,isCrossRepository"
    assert gh.calls == [["pr", "view", "7", "--json", fields]]


DELETE = ["api", "-X", "DELETE", "repos/tbhb-dev/ascm/git/refs/heads/feat/x%20y"]
PR_ARGS = ["pr", "close", "7", "--reason", "r", "--delete-branch"]


def test_delete_branch_deletes_the_same_repo_head_after_closing(gh: Gh) -> None:
    opened.record_opened(root(), ["tbhb-dev/ascm#7"])
    assert close.main(PR_ARGS, env=ENV) == 0
    assert [call[0] for call in gh.calls] == ["pr", "pr", "pr", "api"]
    assert gh.calls[-2][:2] == ["pr", "close"]
    assert gh.calls[-1] == DELETE


def test_delete_branch_skips_a_fork_with_a_note(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    gh.view["isCrossRepository"] = True
    assert close.main(PR_ARGS, env=ENV) == 0
    assert gh.calls[-1][:2] == ["pr", "close"]
    assert "not deleting branch feat/x y: it is on a fork" in capsys.readouterr().out


def test_dry_run_says_it_would_delete_the_branch(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    assert close.main([*PR_ARGS, "--dry-run"], env=ENV) == 0
    assert [call[1] for call in gh.calls] == ["view", "list"]
    out = capsys.readouterr().out
    assert "would delete branch feat/x y: gh api -X DELETE repos/tbhb-dev/ascm" in out


def test_dry_run_says_it_would_skip_a_fork(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    gh.view["isCrossRepository"] = True
    assert close.main([*PR_ARGS, "--dry-run"], env=ENV) == 0
    assert [call[1] for call in gh.calls] == ["view"]
    assert "not deleting branch feat/x y: it is on a fork" in capsys.readouterr().out


def test_without_the_flag_no_branch_is_deleted(gh: Gh) -> None:
    assert close.main(["pr", "close", "7", "--reason", "r"], env=ENV) == 0
    assert all(call[0] != "api" for call in gh.calls)


def test_a_failed_delete_returns_gh_exit_code(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    gh.delete_rc = 1
    assert close.main(PR_ARGS, env=ENV) == 1
    assert "denied" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["boom", "boom\n", ""])
def test_a_failed_delete_passes_gh_stderr_through_unchanged(
    gh: Gh, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    gh.delete_rc = 1
    gh.delete_stderr = text
    assert close.main(PR_ARGS, env=ENV) == 1
    assert capsys.readouterr().err == "warn\n" + text


def test_a_failed_close_skips_the_delete(gh: Gh) -> None:
    gh.close_rc = 4
    assert close.main(PR_ARGS, env=ENV) == 4
    assert all(call[0] != "api" for call in gh.calls)


def test_issue_close_has_no_delete_branch_flag(gh: Gh) -> None:
    argv = ["issue", "close", "3", "--reason", "completed", "--delete-branch"]
    with pytest.raises(SystemExit) as exc:
        close.main(argv, env=ENV)
    assert exc.value.code == 2
    assert gh.calls == []


LIST = [
    "pr", "list", "--repo", "tbhb-dev/ascm", "--base", "feat/x y",
    "--state", "open", "--json", "number",
]  # fmt: skip


def test_delete_branch_lists_open_prs_on_the_head_with_exact_argv(gh: Gh) -> None:
    assert close.main(PR_ARGS, env=ENV) == 0
    assert gh.calls[1] == LIST
    assert gh.calls[2][:2] == ["pr", "close"]


@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_open_dependents_refuse_before_anything_is_posted(
    gh: Gh, dry: list[str]
) -> None:
    gh.list_out = '[{"number": 15}, {"number": 12}]'
    with pytest.raises(SystemExit) as exc:
        close.main([*PR_ARGS, *dry], env=ENV)
    assert exc.value.code == "not closing: open PRs target branch feat/x y: #12, #15"
    assert [call[1] for call in gh.calls] == ["view", "list"]


def test_dry_run_without_dependents_reports_as_before(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    assert close.main([*PR_ARGS, "--dry-run"], env=ENV) == 0
    assert "would delete branch feat/x y" in capsys.readouterr().out
    assert [call[1] for call in gh.calls] == ["view", "list"]


def test_a_failed_list_refuses(gh: Gh) -> None:
    gh.list_rc = 1
    with pytest.raises(SystemExit) as exc:
        close.main(PR_ARGS, env=ENV)
    assert exc.value.code == (
        "not closing: cannot list open PRs targeting feat/x y: no list"
    )
    assert [call[1] for call in gh.calls] == ["view", "list"]


@pytest.mark.parametrize(
    "out", ["not json", "{}", "[1]", '[{"id": 3}]', '[{"number": "3"}]']
)
def test_unparseable_list_output_refuses(gh: Gh, out: str) -> None:
    gh.list_out = out
    with pytest.raises(SystemExit, match="cannot list open PRs"):
        close.main(PR_ARGS, env=ENV)
    assert [call[1] for call in gh.calls] == ["view", "list"]


def test_a_fork_skips_the_dependents_check(gh: Gh) -> None:
    gh.view["isCrossRepository"] = True
    gh.list_rc = 1
    assert close.main(PR_ARGS, env=ENV) == 0
    assert all(call[1] != "list" for call in gh.calls)


def test_without_the_flag_no_list_is_read(gh: Gh) -> None:
    gh.list_rc = 1
    assert close.main(["pr", "close", "7", "--reason", "r"], env=ENV) == 0
    assert all(call[1] != "list" for call in gh.calls)


def test_the_refusal_messages_are_exact(gh: Gh) -> None:
    gh.view["body"] = "Why.\n"
    with pytest.raises(SystemExit) as exc:
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)
    assert exc.value.code == (
        "not closing: the pull request has no Opened-by line (a human's?)"
    )
    with pytest.raises(SystemExit) as exc:
        close.main(["pr", "close", "7", "--reason", "x"], env={})
    assert exc.value.code == "not closing: the caller isn't a recognized agent session"
