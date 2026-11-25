# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for tbhbdev alpha pr close and alpha issue close, with a stubbed gh."""

import json
import subprocess
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

import pytest
from agent_example import settings

from tbhbdev import alpha, userconfig
from tbhbdev.alpha import close, opened
from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from tbhbdev.alpha.agent import model

ENV = {"CLAUDE_CODE_SESSION_ID": "raw-session"}
URL = "https://github.com/tbhb-dev/tools/pull/7"
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
            "author": {"login": "tbhb-agent"},
            "headRefName": "feat/x y",
            "isCrossRepository": False,
        }
    )
    me: str = "tbhb-agent"
    me_rc: int = 0
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
        if argv[:4] == ["api", "--hostname", "github.com", "user"]:
            return subprocess.CompletedProcess(
                argv, self.me_rc, f"{self.me}\n", "no me"
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
    monkeypatch.setattr(pr, "origin_repository", lambda: "tbhb-dev/tools")

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
    opened.record_opened(root(), ["tbhb-dev/tools#7"])
    assert close.main(["pr", "close", "7", "--reason", "superseded"], env=ENV) == 0
    assert gh.calls[0][3:5] == gh.calls[-1][3:5]
    assert gh.calls[-1] == [
        "pr",
        "close",
        "7",
        "--repo",
        "github.com/tbhb-dev/tools",
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


def test_a_pr_without_opened_by_by_the_agent_account_is_closed_on_that_basis(
    gh: Gh,
) -> None:
    gh.view["body"] = "Why.\n"
    assert close.main(["pr", "close", "7", "--reason", "stale"], env=ENV) == 0
    assert ["api", "--hostname", "github.com", "user", "--jq", ".login"] in gh.calls
    assert gh.calls[-1][-1] == (
        "Closed on the basis that its GitHub author is the agent account tbhb-agent"
        f" and it has no Opened-by line.\n\nstale\n\n{trailer_line()}\n"
    )


def test_a_bot_authored_pr_without_opened_by_is_closed_on_that_basis(gh: Gh) -> None:
    gh.view["body"] = "Why.\n"
    gh.view["author"] = {"is_bot": True, "login": "app/tbhb-dev-renovate"}
    assert close.main(["pr", "close", "7", "--reason", "stale"], env=ENV) == 0
    assert gh.calls[-1][-1] == (
        "Closed on the basis that its GitHub author is the bot account"
        f" app/tbhb-dev-renovate and it has no Opened-by line.\n\nstale\n\n"
        f"{trailer_line()}\n"
    )


@pytest.mark.parametrize(
    "author",
    [{"is_bot": True}, {"is_bot": True, "login": ""}, {"is_bot": "yes", "login": "x"}],
)
def test_a_bot_author_needs_a_flag_and_a_login(gh: Gh, author: object) -> None:
    gh.view["body"] = "Why.\n"
    gh.view["author"] = author
    with pytest.raises(SystemExit, match="no Opened-by line"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


@pytest.mark.parametrize(
    "author", [{"login": ""}, {"login": 1}, {}, None, "tbhb-agent"]
)
def test_a_pr_with_no_readable_author_is_refused_without_asking_gh(
    gh: Gh, author: object
) -> None:
    gh.view["body"] = "Why.\n"
    gh.view["author"] = author
    with pytest.raises(SystemExit, match="no Opened-by line"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)
    assert [call[1] for call in gh.calls] == ["view"]


def test_a_pr_by_another_login_is_refused_after_one_account_lookup(gh: Gh) -> None:
    gh.view["body"] = "Why.\n"
    gh.view["author"] = {"login": "tbhb"}
    with pytest.raises(SystemExit, match="no Opened-by line"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)
    assert [call[0] for call in gh.calls] == ["pr", "api"]


def test_a_pr_without_opened_by_is_refused_when_the_account_cannot_be_read(
    gh: Gh,
) -> None:
    gh.view["body"] = "Why.\n"
    gh.me_rc = 1
    with pytest.raises(SystemExit, match="no Opened-by line"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


def test_a_pr_without_opened_by_is_refused_when_the_account_differs(gh: Gh) -> None:
    gh.view["body"] = "Why.\n"
    gh.me = "someone-else"
    with pytest.raises(SystemExit, match="no Opened-by line"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


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
    gh.view["url"] = "https://github.com/tbhb-dev/tools/issues/7"
    with pytest.raises(SystemExit, match="cannot read a pull request from"):
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)


def test_dry_run_prints_the_command_and_closes_nothing(
    gh: Gh, capsys: pytest.CaptureFixture[str]
) -> None:
    opened.record_opened(root(), ["tbhb-dev/tools#7"])
    assert close.main(["pr", "close", "7", "--reason", "r", "--dry-run"], env=ENV) == 0
    assert [call[1] for call in gh.calls] == ["view"]
    assert capsys.readouterr().out.startswith(
        "gh pr close 7 --repo github.com/tbhb-dev/tools --comment "
    )


def test_a_failing_close_returns_gh_exit_code(gh: Gh) -> None:
    opened.record_opened(root(), ["tbhb-dev/tools#7"])
    gh.close_rc = 4
    assert close.main(["pr", "close", "7", "--reason", "r"], env=ENV) == 4


def test_close_issue_with_a_reason_and_a_comment(gh: Gh) -> None:
    gh.view = {"state": "OPEN", "url": "https://github.com/tbhb-dev/tools/issues/3"}
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
        "--repo",
        "github.com/tbhb-dev/tools",
        "--reason",
        "not planned",
        "--comment",
        f"Out of scope.\n\n{trailer_line()}\n",
    ]


def test_close_issue_pins_the_view_to_origin(gh: Gh) -> None:
    gh.view = {"state": "OPEN", "url": "https://github.com/tbhb-dev/tools/issues/3"}
    assert close.main(["issue", "close", "3", "--reason", "completed"], env=ENV) == 0
    assert gh.calls[0][:5] == [
        "issue",
        "view",
        "3",
        "--repo",
        "github.com/tbhb-dev/tools",
    ]


def test_close_issue_without_a_comment_uses_a_default(gh: Gh) -> None:
    gh.view = {"state": "OPEN", "url": "x"}
    assert close.main(["issue", "close", "3", "--reason", "completed"], env=ENV) == 0
    assert gh.calls[-1][-1] == f"Closed.\n\n{trailer_line()}\n"
    assert gh.calls[-1][6] == "completed"


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
    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert last.endswith("must not be blank")
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
    assert parser.description == "Close a PR or manage issues."
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    assert "Close a PR or manage issues." in capsys.readouterr().out


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


@pytest.mark.parametrize(
    ("verb", "arguments"),
    [
        ("link-sub", ("PARENT CHILD",)),
        (
            "block",
            ("--by BLOCKER", "--blocked-by BLOCKER", "--blocking BLOCKED", " ISSUE\n"),
        ),
    ],
)
def test_issue_relationship_usage(
    verb: str, arguments: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        close.main(["issue", verb, "--help"], prog="prog")
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for argument in arguments:
        assert argument in output


def test_issue_relationship_reports_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = "Cannot link example/child#9 under example/parent#7: visibility refused"

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise close.content.Refused(message)

    monkeypatch.setitem(close.RELATIONSHIP_WRITES, "link-sub", refuse)
    assert close.main(["issue", "link-sub", "example/parent#7", "example/child#9"]) == 1
    assert capsys.readouterr() == ("", f"{message}\n")


@pytest.mark.parametrize("case", [({}, True, 0), (None, False, 0), (None, True, 4)])
@pytest.mark.usefixtures("gh")
def test_issue_blocks_using_local_references(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[dict[str, int] | None, bool, int],
) -> None:
    result, dry_run, code = case
    error = subprocess.CalledProcessError(code, "gh", stderr="API error\n")
    write = Mock(return_value=result, side_effect=error if code else None)
    monkeypatch.setitem(close.RELATIONSHIP_WRITES, "block", write)
    argv = ["issue", "block", "#7", "--blocking", "#9"]
    assert close.main([*argv, "--dry-run"] if dry_run else argv) == code
    expected = ("{}\n" if result is not None else "", error.stderr if code else "")
    assert capsys.readouterr() == expected
    write.assert_called_once_with(
        close.content.IssueRef("tbhb-dev", "tools", 9),
        close.content.IssueRef("tbhb-dev", "tools", 7),
        caller="tbhbdev alpha issue",
        dry_run=dry_run,
    )


ISSUE_REPO = "tbhb-dev/work-private"
ISSUE_TITLE = "Improve weekly report delivery"
ISSUE_BODY = "criteria:\n- Reports reach every team\nevidence:\n### Delivery notes\n"
CREATED_ISSUE = f"https://github.com/{ISSUE_REPO}/issues/9\n9\n"


@pytest.fixture
def issue_args(tmp_path: Path) -> list[str]:
    body = tmp_path / "body.md"
    body.write_text(ISSUE_BODY, encoding="utf-8")
    return [
        "issue",
        "create",
        "--repo",
        ISSUE_REPO,
        "--title",
        ISSUE_TITLE,
        "--body-file",
        str(body),
        "--type",
        "Chore",
    ]


@pytest.fixture
def issue_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    repo = f"/repos/{ISSUE_REPO}"
    other = "/repos/tbhb-dev/tools"
    responses: dict[tuple[str, str], close.content.Json] = {
        ("GET", repo): {"full_name": ISSUE_REPO, "visibility": "private"},
        ("GET", other): {"full_name": "tbhb-dev/tools", "visibility": "public"},
        ("POST", f"{repo}/issues"): {"number": 9},
        ("POST", f"{other}/issues"): {"number": 9},
        ("GET", f"{repo}/issues/9"): {"id": 123, "type": {"name": "Task"}},
        ("GET", f"{other}/issues/9"): {"id": 123, "type": {"name": "Chore"}},
        ("GET", f"{repo}/issues/8"): {"type": {"name": "Epic"}},
        ("GET", f"{other}/issues/8"): {"type": {"name": "Epic"}},
        ("POST", f"{repo}/issues/8/sub_issues"): {},
        ("POST", f"{other}/issues/8/sub_issues"): {},
    }

    def respond(
        method: str, path: str, _payload: close.content.Json | None = None
    ) -> close.content.Json:
        return responses[method, path]

    client = Mock(spec=close.content.Client, rest=Mock(side_effect=respond))
    monkeypatch.setattr(close.content, "GhClient", lambda: client)
    monkeypatch.setattr(close.content, "_scan", Mock())
    monkeypatch.setenv("TBHBDEV_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    return client


@pytest.mark.parametrize(
    "case",
    [
        ("criteria: bad", "criteria and verification must use list items (count 1)"),
        ("", "criteria must be 1 to 3 (count 0)"),
        (
            ISSUE_BODY + "blocked-by: another\n",
            "unknown blocked-by target (count 1)",
        ),
    ],
)
@pytest.mark.usefixtures("gh")
def test_issue_create_refuses_invalid_blocks(
    issue_args: list[str],
    issue_client: Mock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str],
) -> None:
    body, finding = case
    (tmp_path / "body.md").write_text(body, encoding="utf-8")
    assert close.main([*issue_args, "--parent", "8"], env=ENV) == 1
    assert capsys.readouterr() == ("", f"Refused issue: {finding}\n")
    issue_client.rest.assert_not_called()


@pytest.mark.parametrize("issue_type", ["Chore", "Research", "Decision"])
@pytest.mark.usefixtures("gh")
def test_issue_create_dry_run(
    issue_args: list[str],
    issue_client: Mock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    issue_type: str,
) -> None:
    body = ISSUE_BODY if issue_type == "Chore" else "Free-form Markdown\n"
    (tmp_path / "body.md").write_text(body, encoding="utf-8")
    assert (
        close.main([*issue_args, "--type", issue_type, "--parent", "8", "--dry-run"])
        == 0
    )
    out, err = capsys.readouterr()
    assert json.loads(out) == {
        "title": ISSUE_TITLE,
        "body": body,
        "type": issue_type,
        "labels": [],
    }
    assert err == (
        "" if issue_type == "Chore" else f"no schema check for type {issue_type}\n"
    )
    issue_client.rest.assert_called_once_with("GET", f"/repos/{ISSUE_REPO}")
    assert not (tmp_path / "audit.jsonl").exists()


@pytest.mark.parametrize("parent", [None, "8", "tbhb-dev/tools#8"])
@pytest.mark.usefixtures("gh")
def test_issue_create_writes_and_audits(
    issue_args: list[str],
    issue_client: Mock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    parent: str | None,
) -> None:
    issue_type = "Chore" if parent is None else "Epic"
    argv = [*issue_args, "--type", issue_type, "--label", "first", "--label", "second"]
    if parent is not None:
        argv.extend(["--parent", parent, "--reason", "Track rollout"])
    assert close.main(argv, env=ENV) == 0
    assert capsys.readouterr() == (CREATED_ISSUE, "")
    rest: Mock = issue_client.rest
    writes = [entry for entry in rest.call_args_list if entry.args[0] != "GET"]
    assert writes[0] == call(
        "POST",
        f"/repos/{ISSUE_REPO}/issues",
        {
            "title": ISSUE_TITLE,
            "body": ISSUE_BODY,
            "type": issue_type,
            "labels": ["first", "second"],
        },
    )
    expected = [("create_issue", "tbhbdev alpha issue create", ISSUE_REPO, 9)]
    if parent is None:
        assert writes[1:] == []
    else:
        parent_repo = "tbhb-dev/tools" if "#" in parent else ISSUE_REPO
        assert writes[1:] == [
            call(
                "POST",
                f"/repos/{parent_repo}/issues/8/sub_issues",
                {"sub_issue_id": 123},
            )
        ]
        caller = "tbhbdev alpha issue create: Track rollout"
        expected = [
            ("create_issue", caller, ISSUE_REPO, 9),
            ("link_sub_issue", caller, parent_repo, 8),
        ]
    entries = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [
        (e["verb"], e["caller"], e["repo"], e["number"])
        for e in map(json.loads, entries)
    ] == expected


@pytest.mark.usefixtures("gh")
def test_issue_create_reports_issue_before_link_refusal(
    issue_args: list[str],
    issue_client: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        close.main(
            [*issue_args, "--repo", "tbhb-dev/tools", "--parent", f"{ISSUE_REPO}#8"],
            env=ENV,
        )
        == 1
    )
    refusal = (
        f"Cannot link tbhb-dev/tools#9 under {ISSUE_REPO}#8: "
        "only a private Feature can have a public leaf child\n"
    )
    output = "https://github.com/tbhb-dev/tools/issues/9\n9\n"
    assert capsys.readouterr() == (output, refusal)
    rest: Mock = issue_client.rest
    assert [args for args in rest.call_args_list if args.args[0] != "GET"] == [
        call(
            "POST",
            "/repos/tbhb-dev/tools/issues",
            {
                "title": ISSUE_TITLE,
                "body": ISSUE_BODY,
                "type": "Chore",
                "labels": [],
            },
        ),
    ]


@pytest.mark.usefixtures("gh")
def test_issue_create_reports_gh_failure(
    issue_args: list[str],
    issue_client: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issue_client.rest = Mock(
        side_effect=subprocess.CalledProcessError(4, "gh", stderr="API error\n")
    )
    assert close.main(issue_args, env=ENV) == 4
    assert capsys.readouterr() == ("", "API error\n")


@pytest.mark.usefixtures("gh")
def test_issue_create_reports_missing_file(
    issue_args: list[str],
    issue_client: Mock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        close.main([*issue_args, "--body-file", str(tmp_path / "missing.md")], env=ENV)
        == 1
    )
    out, err = capsys.readouterr()
    assert not out
    assert "missing.md" in err
    issue_client.rest.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        ("--type", "Other", "invalid choice"),
        ("--repo", "tools", "expected owner/repo"),
        ("--parent", "#8", "expected N or owner/repo#N"),
        ("--parent", "0", "expected owner/repo#N or #N"),
        ("--reason", "", "must not be blank"),
    ],
)
def test_issue_create_rejects_bad_arguments(
    issue_args: list[str],
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str, str],
) -> None:
    flag, value, diagnostic = case
    with pytest.raises(SystemExit) as exc:
        close.main([*issue_args, flag, value], env=ENV)
    assert exc.value.code == 2
    assert diagnostic in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--repo", "--type", "--title", "--body-file"])
def test_issue_create_requires_arguments(
    issue_args: list[str],
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    index = issue_args.index(flag)
    del issue_args[index : index + 2]
    with pytest.raises(SystemExit) as exc:
        close.main(issue_args, env=ENV)
    assert exc.value.code == 2
    assert capsys.readouterr().err.endswith(
        f"the following arguments are required: {flag}\n"
    )


def test_issue_create_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        alpha.main(["issue", "create", "--help"], "prog")
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--repo OWNER/REPO" in output
    assert "--body-file FILE" in output
    assert "--parent N|OWNER/REPO#N" in output


@pytest.mark.usefixtures("gh")
def test_issue_create_requires_task_parent(
    issue_args: list[str],
    issue_client: Mock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "body.md").write_text(
        "verification:\n- Reports arrive on time\n", encoding="utf-8"
    )
    assert close.main([*issue_args, "--type", "Task"], env=ENV) == 1
    assert capsys.readouterr() == (
        "",
        "Refused issue: Task requires a parent (count 0)\n",
    )
    issue_client.rest.assert_not_called()


@pytest.mark.usefixtures("gh")
def test_issue_create_requires_an_agent(
    issue_args: list[str], issue_client: Mock
) -> None:
    with pytest.raises(SystemExit, match="recognized agent session"):
        close.main(issue_args, env={})
    issue_client.rest.assert_not_called()


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
    fields = "state,url,body,author,headRefName,isCrossRepository"
    assert gh.calls == [["pr", "view", "7", "--json", fields]]


def test_view_pins_the_repo_when_given(gh: Gh) -> None:
    close.view("pr", 7, "tbhb-dev/tools")
    (call,) = gh.calls
    assert call[3:5] == ["--repo", "github.com/tbhb-dev/tools"]


def test_comment_is_an_alias_for_reason(gh: Gh) -> None:
    opened.record_opened(root(), ["tbhb-dev/tools#7"])
    assert close.main(["pr", "close", "7", "--comment", "Done."], env=ENV) == 0
    assert gh.calls[-1][-1] == f"Done.\n\n{trailer_line()}\n"


DELETE = ["api", "-X", "DELETE", "repos/tbhb-dev/tools/git/refs/heads/feat/x%20y"]
PR_ARGS = ["pr", "close", "7", "--reason", "r", "--delete-branch"]


def test_delete_branch_deletes_the_same_repo_head_after_closing(gh: Gh) -> None:
    opened.record_opened(root(), ["tbhb-dev/tools#7"])
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
    assert "would delete branch feat/x y: gh api -X DELETE repos/tbhb-dev/tools" in out


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
    "pr", "list", "--repo", "tbhb-dev/tools", "--base", "feat/x y",
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
def test_malformed_list_output_refuses(gh: Gh, out: str) -> None:
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
    gh.view["author"] = {"login": "tbhb"}
    with pytest.raises(SystemExit) as exc:
        close.main(["pr", "close", "7", "--reason", "x"], env=ENV)
    assert exc.value.code == (
        "not closing: the pull request has no Opened-by line (a human's?)"
    )
    with pytest.raises(SystemExit) as exc:
        close.main(["pr", "close", "7", "--reason", "x"], env={})
    assert exc.value.code == "not closing: the caller isn't a recognized agent session"


WORKER = "/opt/worker/bin/worker"
ISSUE = ["issue", "close", "3", "--reason", "completed", "--comment", "Done."]


@dataclass
class Worker:
    """A stub worker lookup: what it was asked, and what it answers."""

    why: str | None = None
    registered: str | None = WORKER
    asked: list[str | None] = field(default_factory=list)


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, gh: Gh) -> Worker:
    stub = Worker()
    gh.view = {"state": "OPEN", "url": "https://github.com/tbhb-dev/tools/issues/3"}

    def refusal(registered: str | None) -> str | None:
        stub.asked.append(registered)
        return stub.why

    def load() -> model.Settings:
        return replace(settings(), worker_path=stub.registered)

    monkeypatch.setattr(close.worker_identity, "refusal", refusal)
    monkeypatch.setattr(close.agent_settings, "load", load)
    return stub


def test_a_matched_worker_closes_an_issue_with_a_closed_by_trailer(
    gh: Gh, worker: Worker
) -> None:
    args = [*ISSUE, "--worker-workflow", "run:7"]
    assert close.main(args, env={}) == 0
    assert gh.calls[-1] == [
        "issue",
        "close",
        "3",
        "--repo",
        "github.com/tbhb-dev/tools",
        "--reason",
        "completed",
        "--comment",
        "Done.\n\nClosed-by: worker/run:7\n",
    ]
    assert worker.asked == [WORKER]


def test_the_worker_trailer_is_never_a_generated_by_or_assisted_by(
    gh: Gh, worker: Worker
) -> None:
    close.main([*ISSUE, "--worker-workflow", "w"], env={})
    comment = gh.calls[-1][-1]
    assert "Generated-by" not in comment
    assert "Assisted-by" not in comment
    assert worker.asked == [WORKER]


def test_the_worker_dry_run_prints_the_trailer(
    gh: Gh, worker: Worker, capsys: pytest.CaptureFixture[str]
) -> None:
    assert close.main([*ISSUE, "--worker-workflow", "w", "--dry-run"], env={}) == 0
    assert "Closed-by: worker/w" in capsys.readouterr().out
    assert [call[:2] for call in gh.calls] == [["issue", "view"]]
    assert worker.asked == [WORKER]


def test_an_unmatched_worker_flag_closes_nothing(gh: Gh, worker: Worker) -> None:
    worker.why = "no ancestor process runs the registered worker /w"
    with pytest.raises(SystemExit) as caught:
        close.main([*ISSUE, "--worker-workflow", "w"], env={})
    assert str(caught.value) == (
        "not closing: --worker-workflow needs the registered worker: "
        "no ancestor process runs the registered worker /w"
    )
    assert gh.calls == []


def test_an_unregistered_worker_is_asked_with_no_path(gh: Gh, worker: Worker) -> None:
    worker.registered = None
    worker.why = "no [agent.worker] path is registered in the config"
    with pytest.raises(SystemExit, match=r"no \[agent\.worker\] path is registered"):
        close.main([*ISSUE, "--worker-workflow", "w"], env={})
    assert worker.asked == [None]
    assert gh.calls == []


def test_an_unreadable_config_closes_nothing(
    gh: Gh, worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def load() -> model.Settings:
        message = "/cfg/config.toml: [agent] table is required"
        raise userconfig.ConfigError(message)

    monkeypatch.setattr(close.agent_settings, "load", load)
    with pytest.raises(SystemExit) as caught:
        close.main([*ISSUE, "--worker-workflow", "w"], env={})
    assert str(caught.value) == "/cfg/config.toml: [agent] table is required"
    assert worker.asked == []
    assert gh.calls == []


def test_no_session_and_no_flag_is_refused_as_before(gh: Gh, worker: Worker) -> None:
    with pytest.raises(SystemExit, match="isn't a recognized agent session"):
        close.main(ISSUE, env={})
    assert worker.asked == []
    assert gh.calls == []


def test_a_harness_session_keeps_its_generated_by_trailer(
    gh: Gh, worker: Worker
) -> None:
    assert close.main([*ISSUE, "--worker-workflow", "w"], env=ENV) == 0
    assert gh.calls[-1][-1] == f"Done.\n\n{trailer_line()}\n"
    assert worker.asked == []


def test_a_harness_session_without_the_flag_is_unchanged(
    gh: Gh, worker: Worker
) -> None:
    assert close.main(ISSUE, env=ENV) == 0
    assert gh.calls[-1][-1] == f"Done.\n\n{trailer_line()}\n"
    assert worker.asked == []


def test_a_pr_close_has_no_worker_path(gh: Gh, worker: Worker) -> None:
    with pytest.raises(SystemExit) as caught:
        close.build_parser("p").parse_args(
            ["pr", "close", "7", "--reason", "r", "--worker-workflow", "w"]
        )
    assert caught.value.code == 2
    with pytest.raises(SystemExit, match="isn't a recognized agent session"):
        close.main(["pr", "close", "7", "--reason", "r"], env={})
    assert worker.asked == []
    assert gh.calls == []


def test_issue_create_has_no_worker_path() -> None:
    with pytest.raises(SystemExit) as caught:
        close.build_parser("p").parse_args(
            ["issue", "create", "--worker-workflow", "w"]
        )
    assert caught.value.code == 2


def test_the_parser_reads_the_worker_workflow() -> None:
    parser = close.build_parser("p")
    assert parser.parse_args([*ISSUE, "--worker-workflow", "a:b"]).worker_workflow == (
        "a:b"
    )
    assert parser.parse_args(ISSUE).worker_workflow is None
    with pytest.raises(SystemExit):
        parser.parse_args([*ISSUE, "--worker-workflow", "a b"])


def test_issue_close_help_names_the_flag_and_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        close.main(["issue", "close", "--help"], prog="prog", env=ENV)
    text = " ".join(capsys.readouterr().out.split())
    assert (
        "--worker-workflow ID with no agent session, close as worker/ID: the "
        "caller must descend from the executable in the config's [agent.worker] "
        "table"
    ) in text
