# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for tbhbdev alpha guard-gh-pr, the gh pr guard hook."""

import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from fake_tool import pythonpath
from hypothesis import given
from hypothesis import strategies as st

from tbhbdev.alpha import close, install, opened, pull_request, squash_merge
from tbhbdev.alpha import guard_gh_pr as guard
from tbhbdev.alpha import identity as ai

DATA = Path(__file__).parent / "testdata" / "alpha"
SRC = Path(guard.__file__).parents[2]

CREATE = (
    "`gh pr create` is blocked. Write .github/PULL_REQUEST_DRAFT.md from "
    ".github/PULL_REQUEST_TEMPLATE.md, check it with `tbhbdev alpha pr check`, "
    "then open the PR with `tbhbdev alpha pr create`."
)
EDIT = (
    "`gh pr edit` is blocked. Update .github/PULL_REQUEST_DRAFT.md, "
    "then run `tbhbdev alpha pr edit .github/PULL_REQUEST_DRAFT.md --pr {n}`."
)
READY = "`gh pr ready` is blocked. Run `tbhbdev alpha pr ready {n}` instead."
PR_CLOSE = (
    "`gh pr close` is blocked. From the repository clone, run "
    "`tbhbdev alpha pr close {n} --reason TEXT` (no --repo)."
)
ISSUE_CLOSE = (
    "`gh issue close` is blocked. From the repository clone, run "
    "`tbhbdev alpha issue close {n} --reason completed|not-planned "
    "[--comment TEXT]` (no --repo)."
)
RERUN = (
    "`gh run rerun` is blocked. Run `tbhbdev alpha pr rerun N`, where N is the "
    "pull request number, to rerun the failed jobs of its latest run."
)
DELETE_BRANCH = (
    "A `gh api` branch delete is blocked. For a closed or merged PR, run "
    "`tbhbdev alpha pr delete-branch {n}` instead."
)
THREAD_HINT = (
    "A `gh api graphql` review thread mutation is blocked. From the repository "
    "clone, run `tbhbdev alpha review {command} <PR> <THREAD>`. Use "
    "`tbhbdev alpha review threads <PR>` to find the thread ID."
)
MERGE = (
    "`gh pr merge` is blocked. Once the PR is approved, run "
    "`tbhbdev alpha merge {n}` (add `--dry-run` to preview the commit message first)."
)

BLOCKED = [
    "gh pr create",
    "gh pr create --title x --body y",
    "gh pr edit 3",
    "gh pr merge 9 --squash",
    "  gh   pr   merge 9",
    "GH_CONFIG_DIR=x gh pr merge 1",
    "FOO=1 BAR='a b' gh pr merge 1",
    "env FOO=1 gh pr create",
    "env -i gh pr create",
    "command gh pr merge 1",
    "exec gh pr merge 1",
    "sudo -n gh pr merge 1",
    "nohup gh pr merge 1",
    "time gh pr merge 1",
    "/opt/homebrew/bin/gh pr merge 1",
    "cd a && gh pr merge 1",
    "cd a; gh pr merge 1",
    "false || gh pr merge 1",
    "echo x | gh pr merge 1",
    "echo x & gh pr merge 1",
    "(gh pr merge 5)",
    "( cd a && gh pr merge 5 )",
    "{ gh pr merge 5; }",
    "x=$(gh pr create)",
    'echo "$(gh pr merge 1)"',
    'echo "a $(echo b) $(gh pr merge 1)"',
    "echo `gh pr merge 1`",
    'echo "`gh pr merge 1`"',
    "echo $(echo $(gh pr merge 1))",
    "gh -R a/b pr edit 3",
    "gh --repo a/b pr edit 3",
    "gh --repo=a/b pr edit 3",
    "gh --hostname h pr edit 3",
    "gh -R a/b -R c/d pr merge 3",
    "bash -c 'gh pr merge 1'",
    'sh -c "gh pr merge 1"',
    "bash -lc 'cd x && gh pr merge 1'",
    "bash -e -c 'gh pr merge 1'",
    "/bin/bash -c 'bash -c \"gh pr merge 1\"'",
    "eval 'gh pr merge 1'",
    "eval gh pr merge 1",
    "xargs gh pr merge",
    "if true; then gh pr merge 1; fi",
    "for i in 1; do gh pr merge $i; done",
    "while false; do :; done; gh pr merge 1",
    "! gh pr merge 1",
    "echo hi\ngh pr create",
    "echo hi \\\n && gh pr create",
    "cat <<EOF\nbody\nEOF\ngh pr merge 1",
    "cat <<-'EOF'\n\tbody\n\tEOF\ngh pr merge 1",
    "cat <<EOF >out && gh pr merge 1\nbody\nEOF",
    "echo 'a' # c\ngh pr merge 1",
    "echo \\' ; gh pr merge 1",
    'echo "\\"" ; gh pr merge 1',
    "echo hi # gh pr view\ngh pr merge 1",
]

ALLOWED = [
    "",
    "ls",
    "gh pr view 1",
    "gh pr list",
    "gh pr checks 1",
    "gh pr diff 1",
    "gh pr status",
    "gh pr",
    "gh pr --help",
    "gh -R a/b",
    "gh -R",
    "gh api repos/a/b/pulls/1/merge -X PUT",
    "gh issue create",
    "mise run pr -- create",
    "mise run pr -- edit --pr 3",
    "mise run merge -- 3 --dry-run",
    "uv run tbhbdev alpha pr create",
    "uv run tbhbdev alpha merge 3",
    "git commit -m 'docs: mention gh pr merge'",
    'git commit -m "docs: mention gh pr merge"',
    "git commit -m 'a' -m 'gh pr create'",
    "echo 'gh pr merge 1'",
    'echo "gh pr merge 1"',
    "grep -r 'gh pr create' docs",
    "echo gh pr merge # not run",
    "cat <<EOF\ngh pr merge 1\nEOF",
    "cat <<-EOF\n\tgh pr merge 1\n\tEOF",
    "git commit -m \"$(cat <<'EOF'\ndocs: gh pr merge note\n\ngh pr create\nEOF\n)\"",
    "cat <<EOF",
    "cat << EOF\ngh pr merge 1\nEOF",
    "bash script.sh gh pr merge",
    "bash -c",
    "bash -e",
    "bash --norc -c 'echo ok'",
    "eval",
    "env",
    "env -i",
    "echo $((1 + 2))",
    "gh pr view 1 && gh pr checks 1 | cat",
    "echo 'unterminated gh pr merge",
    "echo \\",
]


def run_find(text: str) -> guard.Match | None:
    """Scan one command."""
    return guard.find(text)


@pytest.mark.parametrize("text", BLOCKED)
def test_blocked(text: str) -> None:
    assert run_find(text) is not None


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed(text: str) -> None:
    assert run_find(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gh pr create --title x", CREATE),
        ("gh pr edit 12", EDIT.format(n=12)),
        ("gh pr edit --title x", EDIT.format(n="<N>")),
        ("gh -R a/b pr edit '#34' --title x", EDIT.format(n=34)),
        ("gh pr edit https://github.com/a/b/pull/56", EDIT.format(n=56)),
        ("gh pr merge 7 --squash", MERGE.format(n=7)),
        ("gh pr merge --auto 8", MERGE.format(n=8)),
        ("gh pr merge", MERGE.format(n="<N>")),
        ("gh pr merge feature-branch", MERGE.format(n="<N>")),
        ("gh pr close 5 --repo a/b -c done", PR_CLOSE.format(n=5)),
        ("gh pr close", PR_CLOSE.format(n="<N>")),
        ("gh issue close 6 --reason 'not planned'", ISSUE_CLOSE.format(n=6)),
        ("gh -R a/b issue close 6", ISSUE_CLOSE.format(n=6)),
        ("gh run rerun 123 --failed", RERUN),
        (
            "gh api -X DELETE repos/a/b/git/refs/heads/feat/x",
            DELETE_BRANCH.format(n="<N>"),
        ),
        (
            "gh api repos/{owner}/{repo}/git/refs/heads/x --method delete",
            DELETE_BRANCH.format(n="<N>"),
        ),
        (
            "gh api graphql -f 'query=mutation { resolveReviewThread(input: $input) }'",
            THREAD_HINT.format(command="resolve"),
        ),
        ("gh pr ready 399", READY.format(n=399)),
        ("gh pr ready", READY.format(n="<N>")),
        (
            "gh pr ready 399 --undo",
            (
                "`gh pr ready` is blocked. `tbhbdev alpha pr ready` has no undo. "
                "Ask the operator to return the PR to draft."
            ),
        ),
    ],
)
def test_hint(text: str, expected: str) -> None:
    found = run_find(text)
    assert found is not None
    assert guard.hint(found) == expected


def test_branch_delete_match_preserves_api_arguments() -> None:
    args = ["-X", "DELETE", "repos/a/b/git/refs/heads/x"]
    assert guard.gh_match(["-R", "a/b", "api", *args]) == ("delete-branch", args, "api")


@pytest.mark.parametrize(
    "text",
    [
        'echo "gh pr close 1"',
        "echo 'gh issue close 1; gh run rerun 2'",
        "gh issue list",
        "gh run view 1",
        "gh api repos/a/b/git/refs/heads/x",
        "gh api -X GET repos/a/b/git/refs/heads/x",
        "gh api -X DELETE repos/a/b/issues/1",
        "gh api -X DELETE",
        "gh api",
        "gh api graphql -f 'query=query { viewer { login } }'",
        "gh api graphql -f 'query={ search(query: \"resolveReviewThread(a)\") }'",
        "gh api graphql -f 'input=resolveReviewThread(...)'",
        "gh api repos/a/b/issues -f 'query=mutation { resolveReviewThread(...) }'",
    ],
)
def test_new_hints_leave_other_commands_alone(text: str) -> None:
    assert run_find(text) is None


@pytest.mark.parametrize(
    ("command", "named"),
    [
        ("gh pr create", "tbhbdev alpha pr create"),
        ("gh pr edit 3", "tbhbdev alpha pr edit .github/PULL_REQUEST_DRAFT.md --pr 3"),
        ("gh pr merge 3", "tbhbdev alpha merge 3"),
        ("gh pr ready 3", "tbhbdev alpha pr ready 3"),
        ("gh pr close 3", "tbhbdev alpha pr close 3 --reason TEXT"),
        ("gh issue close 3", "tbhbdev alpha issue close 3 --reason"),
        ("gh run rerun 9", "tbhbdev alpha pr rerun N"),
        (
            "gh api -X DELETE repos/a/b/git/refs/heads/x",
            "tbhbdev alpha pr delete-branch",
        ),
        (
            "gh api graphql --field='query=mutation { unresolveReviewThread(a: $i) }'",
            "tbhbdev alpha review unresolve <PR> <THREAD>",
        ),
    ],
)
def test_every_denial_names_the_command_and_the_alpha_footer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    named: str,
) -> None:
    stdin = json.dumps(with_command(CLAUDE_PAYLOAD, command))
    _, out, _ = run_main(monkeypatch, capsys, stdin)
    message = reason(out)
    assert message is not None
    first, *_, last = message.split("\n")
    assert first == "tbhbdev hint:"
    assert named in message
    assert last == (
        "Use `tbhbdev alpha` for GitHub writes: `tbhbdev alpha --help` lists the "
        "commands, and `tbhbdev alpha session-context` reports this session's context."
    )


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'gh pr close 5'",
        "git commit -m 'gh issue close 5'",
        "git commit -m 'gh run rerun 5'",
        "git commit -m 'gh api -X DELETE repos/a/b/git/refs/heads/x'",
    ],
)
def test_a_lookalike_in_a_commit_message_passes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    stdin = json.dumps(with_command(CLAUDE_PAYLOAD, command))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_first_match_wins() -> None:
    found = run_find("gh pr merge 1; gh pr create")
    assert found is not None
    assert found.sub == "merge"


def test_scanner_backtick_nested_in_dollar_paren_restores_quotes() -> None:
    # The quote opened before `$(` is restored at `)`, so the text after it is
    # still quoted and is not a command.
    assert run_find('echo "$(true) ; gh pr merge 1"') is None


def test_unbalanced_close_paren_is_harmless() -> None:
    assert run_find(") gh pr merge 1") is not None


def test_heredoc_without_closing_line_swallows_the_rest() -> None:
    assert run_find("cat <<EOF\ngh pr merge 1") is None


def test_here_string_is_not_a_heredoc() -> None:
    assert run_find("cat <<< word\ngh pr merge 1") is not None


def test_two_heredocs_on_one_line() -> None:
    text = "cat <<A <<B\nx\nA\ny\nB\ngh pr merge 1"
    assert run_find(text) is not None
    assert run_find("cat <<A <<B\nx\nA\ngh pr merge 1\nB\n") is None


# --- payloads and output contracts -----------------------------------------

CLAUDE_PAYLOAD = json.loads((DATA / "claude-pretooluse-bash-main.json").read_text())
CODEX_PAYLOAD = json.loads((DATA / "codex-pretooluse-bash.json").read_text())
AGY_PAYLOAD = json.loads((DATA / "agy-pretooluse-run-command.json").read_text())
AGY_WRITE = json.loads((DATA / "agy-pretooluse-write.json").read_text())


def with_command(payload: dict[str, object], text: str) -> dict[str, object]:
    """A recorded payload with its shell command replaced."""
    if "toolCall" in payload:
        call = cast("dict[str, object]", payload["toolCall"])
        args = cast("dict[str, object]", call["args"])
        return {**payload, "toolCall": {**call, "args": {**args, "CommandLine": text}}}
    inputs = cast("dict[str, object]", payload["tool_input"])
    return {**payload, "tool_input": {**inputs, "command": text}}


@pytest.fixture(autouse=True)
def tbhb_dev(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
    """Every payload's repository counts as a tbhb-dev one; record each ask."""
    asked: list[Path | None] = []

    def in_tbhb_dev(cwd: Path | None = None) -> bool:
        asked.append(cwd)
        return True

    monkeypatch.setattr(guard, "in_tbhb_dev", in_tbhb_dev)
    return asked


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
) -> tuple[int, str, str]:
    """Run ``main`` on stdin; return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = guard.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD])
def test_claude_and_codex_deny_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "cd x && gh pr create"))
    code, out, err = run_main(monkeypatch, capsys, stdin)
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{guard.HINT_LINE}\n{CREATE}\n{guard.FOOTER}",
        }
    }


def test_antigravity_deny_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin = json.dumps(with_command(AGY_PAYLOAD, "gh pr merge 4"))
    code, out, err = run_main(monkeypatch, capsys, stdin)
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "decision": "deny",
        "reason": f"{guard.HINT_LINE}\n{MERGE.format(n=4)}\n{guard.FOOTER}",
    }


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_recorded_payloads_allow_their_own_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    code, out, err = run_main(monkeypatch, capsys, json.dumps(payload))
    assert (code, out, err) == (0, "", "")


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_gh_pr_view_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "gh pr view 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_agy_non_command_tool_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(monkeypatch, capsys, json.dumps(AGY_WRITE)) == (0, "", "")


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "null",
        "[1]",
        "{}",
        '{"tool_input": null}',
        '{"tool_input": {"command": 7}}',
        '{"tool_input": []}',
        '{"toolCall": null}',
        '{"toolCall": {"args": null}}',
        '{"toolCall": {"args": {"CommandLine": 7}}}',
        '{"toolCall": {"args": "gh pr merge 1"}}',
    ],
)
def test_malformed_input_fails_open_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_internal_error_fails_open_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_text: str) -> guard.Match | None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(guard, "find", boom)
    stdin = json.dumps(with_command(CLAUDE_PAYLOAD, "gh pr merge 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CODEX_PAYLOAD, AGY_PAYLOAD])
def test_the_gate_asks_about_the_payloads_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tbhb_dev: list[Path | None],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, "gh pr merge 1"))
    run_main(monkeypatch, capsys, stdin)
    assert tbhb_dev == [Path("/nonexistent/work")]


def test_outside_tbhb_dev_even_a_blocked_command_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def in_tbhb_dev(_cwd: Path | None) -> bool:
        return False

    monkeypatch.setattr(guard, "in_tbhb_dev", in_tbhb_dev)
    stdin = json.dumps(with_command(CLAUDE_PAYLOAD, "gh pr merge 1"))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def origin_repo(path: Path, origin: str) -> Path:
    """A git repository at `path` with `origin` as its origin remote."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
    )
    return path


@pytest.mark.parametrize(
    ("payload", "key"),
    [
        (CLAUDE_PAYLOAD, "hookSpecificOutput"),
        (AGY_PAYLOAD, "decision"),
    ],
)
@pytest.mark.parametrize(
    ("origin", "blocked"),
    [
        ("git@github.com:tbhb-dev/example.git", True),
        ("https://github.com/tbhb-dev/example", True),
        ("git@github.com:someone/example.git", False),
    ],
)
def test_runs_as_tbhbdev_alpha_guard_gh_pr(
    tmp_path: Path,
    payload: dict[str, object],
    key: str,
    origin: str,
    *,
    blocked: bool,
) -> None:
    repo = origin_repo(tmp_path / "repo", origin)
    data = with_command(payload, "gh pr merge 1")
    data = {k: v for k, v in data.items() if k not in {"cwd", "workspacePaths"}}
    data["workspacePaths" if "toolCall" in data else "cwd"] = (
        [str(repo)] if "toolCall" in data else str(repo)
    )
    env = {**os.environ, "PYTHONPATH": pythonpath(SRC), "HOME": str(tmp_path)}
    env.pop("MUTANT_UNDER_TEST", None)
    done = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "alpha", "guard-gh-pr"],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
    )
    assert (done.returncode, done.stderr) == (0, "")
    assert (key in json.loads(done.stdout)) if blocked else done.stdout == ""


# --- properties ------------------------------------------------------------

WORD = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Nd"), whitelist_characters="-_./"
    ),
    min_size=1,
    max_size=8,
).filter(lambda w: not w.startswith(("-", "#")) and "=" not in w)
SEP = st.sampled_from(["; ", " && ", " || ", " | ", " & ", "\n", " ; "])
SUB = st.sampled_from(guard.SUBCOMMANDS)
LEAD = st.sampled_from(
    ["", "FOO=1 ", "env ", "env -i A=b ", "sudo ", "time ", "command ", "! ", "{ "]
)
GH = st.sampled_from(["gh", "/usr/bin/gh", "gh -R a/b", "gh --repo a/b", "gh -q"])


@given(before=st.lists(WORD, max_size=3), sep=SEP, lead=LEAD, gh=GH, sub=SUB)
def test_property_gh_pr_after_any_boundary_is_blocked(
    before: list[str], sep: str, lead: str, gh: str, sub: str
) -> None:
    prefix = " ".join(["echo", *before]) + sep
    found = run_find(f"{prefix}{lead}{gh} pr {sub} 5")
    assert found is not None
    assert found.sub == sub


@given(sub=SUB, wrapper=st.sampled_from(["bash -c", "sh -c", "zsh -lc", "eval"]))
@pytest.mark.parametrize("quote", ["'", '"'])
def test_property_shell_wrappers_are_scanned(
    sub: str, wrapper: str, quote: str
) -> None:
    found = run_find(f"{wrapper} {quote}cd x; gh pr {sub} 5{quote}")
    assert found is not None
    assert found.sub == sub


@given(words=st.lists(WORD, max_size=4), sub=SUB, quote=st.sampled_from(["'", '"']))
def test_property_quoted_text_of_a_plain_command_is_not_run(
    words: list[str], sub: str, quote: str
) -> None:
    arg = f"{quote}{' '.join(words)} gh pr {sub} 5{quote}"
    assert run_find(f"git commit -m {arg}") is None
    assert run_find(f"echo {arg}") is None


@given(
    verb=st.sampled_from(["view", "list", "checks", "diff", "status"]),
    gh=GH,
)
def test_property_other_pr_subcommands_are_allowed(verb: str, gh: str) -> None:
    assert run_find(f"{gh} pr {verb} 5") is None


@given(st.text(max_size=80))
def test_property_scanner_never_raises(text: str) -> None:
    run_find(text)


@given(st.text(max_size=80))
def test_property_main_is_silent_without_a_match(text: str) -> None:
    payload = json.dumps({"tool_input": {"command": text}})
    found = run_find(text)
    out = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(out):
        assert guard.main() == 0
    assert (out.getvalue() != "") == (found is not None)


# --- harness configs -------------------------------------------------------


def guard_groups(groups: list[dict[str, object]]) -> list[dict[str, object]]:
    """The PreToolUse groups that run the guard."""
    return [
        g
        for g in groups
        if any(
            "alpha guard-gh-pr" in h["command"]
            for h in cast("list[dict[str, str]]", g["hooks"])
        )
    ]


@pytest.mark.parametrize(
    ("groups", "matcher"),
    [
        (install.claude_hooks()["PreToolUse"], "Bash"),
        (install.codex_hooks()["PreToolUse"], "^Bash$"),
        (
            cast(
                "dict[str, list[dict[str, object]]]",
                install.agy_hooks()["tbhbdev-alpha-pull-request"],
            )["PreToolUse"],
            "run_command",
        ),
    ],
)
def test_every_harness_config_runs_the_guard(
    groups: list[dict[str, object]], matcher: str
) -> None:
    (group,) = guard_groups(groups)
    assert group["matcher"] == matcher
    (handler,) = cast("list[dict[str, str]]", group["hooks"])
    assert handler["command"] == "tbhbdev alpha guard-gh-pr # tbhbdev alpha install"


# --- approval routes -------------------------------------------------------

ROOT = ai.sha256_64("session-raw")
TARGET = "tbhb-dev/tools#7"
APPROVE = "tbhb-approve tools 7 abc123"
GH_ROUTES = [
    ("gh pr review 7 --approve -R tbhb-dev/tools", "`gh pr review --approve`"),
    (
        "gh api -X POST repos/tbhb-dev/tools/pulls/7/reviews -f event=APPROVE",
        "a `gh api` APPROVE review",
    ),
]
CLAUDE_CHILD = json.loads((DATA / "claude-pretooluse-bash-child.json").read_text())


@pytest.fixture
def isolated_opened_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def reason(out: str) -> str | None:
    """The deny reason of a Claude or Codex decision; None for silence."""
    if not out:
        return None
    output = json.loads(out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    return cast("str", output["permissionDecisionReason"])


def guard_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
    text: str,
) -> str | None:
    code, out, err = run_main(
        monkeypatch, capsys, json.dumps(with_command(payload, text))
    )
    assert (code, err) == (0, "")
    return reason(out)


GH_DENIED = (
    "An agent can't approve a pull request through gh, whoever opened it. "
    "Approval comes only from `tbhb-approve`, run by the coordinator. "
    "Post the verdict as a COMMENT review instead: it refuses {route}."
)


@pytest.mark.usefixtures("isolated_opened_ledger")
@pytest.mark.parametrize(("text", "route"), GH_ROUTES)
@pytest.mark.parametrize("payload", [CLAUDE_PAYLOAD, CLAUDE_CHILD, CODEX_PAYLOAD])
def test_no_session_approves_through_gh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
    text: str,
    route: str,
) -> None:
    expected = GH_DENIED.format(route=route)
    assert guard_reason(monkeypatch, capsys, payload, text) == expected
    opened.record_opened(ROOT, [TARGET])
    assert guard_reason(monkeypatch, capsys, payload, text) == expected


@pytest.mark.usefixtures("isolated_opened_ledger")
@pytest.mark.parametrize(
    "payload",
    [
        CLAUDE_PAYLOAD,
        CLAUDE_CHILD,
        CODEX_PAYLOAD,
        AGY_PAYLOAD,
        {"tool_input": {}},
    ],
)
def test_tbhb_approve_passes_regardless_of_which_session_opened_the_pr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    stdin = json.dumps(with_command(payload, APPROVE))
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")
    opened.record_opened(ROOT, [TARGET])
    assert run_main(monkeypatch, capsys, stdin) == (0, "", "")


def test_a_crash_while_matching_an_approval_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_text: str, _cwd: Path | None) -> list[guard.Approval]:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(guard, "approvals", boom)
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, GH_ROUTES[0][0]) is None
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, "gh pr merge 1") == (
        f"{guard.HINT_LINE}\n{MERGE.format(n=1)}\n{guard.FOOTER}"
    )


def test_approval_rules_apply_outside_tbhb_dev(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def in_tbhb_dev(_cwd: Path | None) -> bool:
        return False

    monkeypatch.setattr(guard, "in_tbhb_dev", in_tbhb_dev)
    text, route = GH_ROUTES[0]
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, text) == GH_DENIED.format(
        route=route
    )
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, "gh pr merge 1") is None


def test_gh_approval_is_denied_alongside_tbhb_approve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text, route = GH_ROUTES[0]
    assert guard_reason(
        monkeypatch, capsys, CLAUDE_PAYLOAD, f"{APPROVE} && {text}"
    ) == GH_DENIED.format(route=route)


@pytest.mark.parametrize(
    "text",
    [
        "gh pr review 7 -R tbhb-dev/tools",
        "gh pr review 7 -R tbhb-dev/tools --comment -b ok",
        "gh pr review 7 -R tbhb-dev/tools -b -a",
        "gh pr review 7 -R tbhb-dev/tools -ba",
        "gh pr review 7 -R tbhb-dev/tools --request-changes -b no",
        "gh pr view 7 -R tbhb-dev/tools",
        "gh api repos/tbhb-dev/tools/pulls/7/reviews",
        "gh api -X GET repos/tbhb-dev/tools/pulls/7/reviews -f event=APPROVE",
        "gh api --method=GET repos/tbhb-dev/tools/pulls/7/reviews -F event=APPROVE",
        "gh api -X POST repos/tbhb-dev/tools/pulls/7/reviews -f event=COMMENT",
        "gh api repos/tbhb-dev/tools/pulls/7/reviews -f body=APPROVE",
        "gh api repos/tbhb-dev/tools/pulls/7/reviews -f event",
        "gh api -X POST repos/tbhb-dev/tools/pulls/7/reviews --input f.json",
        "gh api repos/tbhb-dev/tools/pulls/7/comments -f event=APPROVE",
        "gh api repos/tbhb-dev/tools/pulls/7/merge -X PUT -f event=APPROVE",
        "gh api repos/tbhb-dev/tools/issues/7/reviews -f event=APPROVE",
        "gh api graphql -f query=addPullRequestReview",
        "gh -R tbhb-dev/tools api -f event=APPROVE",
        "git commit -m 'gh pr review 7 --approve -R tbhb-dev/tools'",
        "gh issue view 7",
        "gh pr",
    ],
)
def test_other_review_calls_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    assert guard.approvals(text, None) == []
    assert guard_reason(monkeypatch, capsys, CLAUDE_PAYLOAD, text) is None


A_REVIEW = "`gh pr review --approve`"
AN_API = "a `gh api` APPROVE review"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gh pr review 7 --approve -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh pr review 7 -a --repo tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh pr review 7 -a --repo=tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh -R tbhb-dev/tools pr review '#7' --approve", (A_REVIEW, TARGET)),
        ("gh --repo github.com/tbhb-dev/tools pr review 7 -a", (A_REVIEW, TARGET)),
        ("gh -R x/y pr review -R tbhb-dev/tools 7 -a", (A_REVIEW, TARGET)),
        ("gh pr review -b ok -a 7 -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh pr review --body-file f -a 7 -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh pr review -F f --approve=true 7 -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh pr review -ab ok 7 -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        (
            "gh pr review https://github.com/tbhb-dev/tools/pull/7 --approve",
            (A_REVIEW, TARGET),
        ),
        (
            "gh pr review -R x/y https://github.com/tbhb-dev/tools/pull/7/files -a",
            (A_REVIEW, TARGET),
        ),
        ("gh pr review --approve -R tbhb-dev/tools", (A_REVIEW, None)),
        ("gh pr review my-branch --approve -R tbhb-dev/tools", (A_REVIEW, None)),
        ("gh pr review 7 --approve -R tools", (A_REVIEW, None)),
        (
            "gh api -X POST repos/tbhb-dev/tools/pulls/7/reviews -f event=APPROVE",
            (AN_API, TARGET),
        ),
        (
            "gh api /repos/tbhb-dev/tools/pulls/7/reviews -F event=APPROVE",
            (AN_API, TARGET),
        ),
        (
            "gh api repos/tbhb-dev/tools/pulls/7/reviews --field=event=approve",
            (AN_API, TARGET),
        ),
        (
            "gh api repos/tbhb-dev/tools/pulls/7/reviews --raw-field 'event = APPROVE'",
            (AN_API, TARGET),
        ),
        (
            'gh api repos/tbhb-dev/tools/pulls/7/reviews -f"event=APPROVE" -XPOST',
            (AN_API, TARGET),
        ),
        (
            (
                "gh api --method post -H x "
                "repos/tbhb-dev/tools/pulls/7/reviews/9/events"
                " -f event=APPROVE"
            ),
            (AN_API, TARGET),
        ),
        (
            (
                "gh api https://api.github.com/repos/tbhb-dev/tools/pulls/7/reviews/"
                " -f body=x -f event=APPROVE"
            ),
            (AN_API, TARGET),
        ),
        (
            "gh api 'repos/tbhb-dev/tools/pulls/7/reviews?x=1' -f event=APPROVE",
            (AN_API, TARGET),
        ),
        (
            "gh api -X POST repos/tbhb-dev/tools/pulls/$N/reviews -f event=APPROVE",
            (AN_API, None),
        ),
        ("env A=b sudo gh pr review 7 -a -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        ('echo "$(gh pr review 7 -a -R tbhb-dev/tools)"', (A_REVIEW, TARGET)),
    ],
)
def test_approval_routes_and_targets(
    text: str, expected: tuple[str, str | None]
) -> None:
    assert guard.approvals(text, None) == [guard.Approval(*expected)]


def test_the_repository_comes_from_the_hook_cwd_origin(tmp_path: Path) -> None:
    repo = origin_repo(tmp_path / "repo", "git@github.com:tbhb-dev/tools.git")
    assert guard.approvals("gh pr review 7 -a", repo) == [
        guard.Approval(A_REVIEW, TARGET)
    ]
    text = "gh api repos/{owner}/{repo}/pulls/7/reviews -f event=APPROVE"
    assert guard.approvals(text, repo) == [guard.Approval(AN_API, TARGET)]
    text = "gh api repos/{owner}/tools/pulls/7/reviews -f event=APPROVE"
    assert guard.approvals(text, repo) == [guard.Approval(AN_API, TARGET)]
    text = "gh api repos/tbhb-dev/{repo}/pulls/7/reviews -f event=APPROVE"
    assert guard.approvals(text, repo) == [guard.Approval(AN_API, TARGET)]
    assert guard.approvals("gh pr review 7 -a -R o/r", repo) == [
        guard.Approval(A_REVIEW, "o/r#7")
    ]
    https = origin_repo(tmp_path / "https", "https://github.com/tbhb-dev/tools/\n")
    assert guard.origin_repo(https) == "tbhb-dev/tools"


def test_the_repository_comes_from_the_push_url_of_a_mirror_clone(
    tmp_path: Path,
) -> None:
    repo = origin_repo(tmp_path / "repo", str(tmp_path / "mirror.git"))
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "remote.origin.pushurl",
            "https://github.com/tbhb-dev/tools.git",
        ],
        check=True,
    )
    assert guard.origin_repo(repo) == "tbhb-dev/tools"


def test_without_an_origin_the_target_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert guard.origin_repo(tmp_path / "plain") is None
    assert guard.approvals("gh pr review 7 -a", tmp_path / "plain") == [
        guard.Approval(A_REVIEW, None)
    ]
    text = "gh api repos/{owner}/{repo}/pulls/7/reviews -f event=APPROVE"
    assert guard.approvals(text, tmp_path / "plain") == [guard.Approval(AN_API, None)]


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("o/r", "o/r"),
        ("github.com/o/r", "o/r"),
        ("https://github.com/o/r.git/", "o/r"),
        ("r", None),
        ("o/", None),
        ("/r", None),
        ("", None),
    ],
)
def test_repo_name(value: str, name: str | None) -> None:
    assert guard.repo_name(value) == name


@pytest.mark.parametrize(
    ("words", "values"),
    [
        (["-X", "POST"], ["POST"]),
        (["-XPOST"], ["POST"]),
        (["--method", "POST"], ["POST"]),
        (["--method=POST"], ["POST"]),
        (["--methods=POST"], []),
        (["--method"], []),
        (["-X", "a", "x", "-X", "b"], ["a", "b"]),
    ],
)
def test_flag_values(words: list[str], values: list[str]) -> None:
    assert guard.flag_values(words, {"-X", "--method"}, {"-X", "--method"}) == values


@pytest.mark.parametrize(
    ("word", "approve"),
    [
        ("--approve", True),
        ("--approve=false", True),
        ("--approved", False),
        ("-a", True),
        ("-ca", True),
        ("-ba", False),
        ("-Fa", False),
        ("-Ra", False),
        ("-c", False),
        ("--a", False),
        ("a", False),
    ],
)
def test_approves(word: str, *, approve: bool) -> None:
    assert guard.approves(word) is approve


def test_split_args_drops_flag_values() -> None:
    words = ["-b", "x", "--body=y", "7", "-a", "-R", "o/r", "-ab", "v", "-bz", "z"]
    assert guard.split_args(words, guard.REVIEW_VALUE_FLAGS) == (
        ["-b", "--body=y", "-a", "-R", "-ab", "-bz"],
        ["7", "z"],
    )


@pytest.mark.parametrize(
    ("word", "takes"),
    [
        ("-b", True),
        ("--body", True),
        ("--body=x", False),
        ("--body-x", False),
        ("-ab", True),
        ("-ba", False),
        ("-Rx/b", False),
        ("-ca", False),
        ("-", False),
    ],
)
def test_takes_value(word: str, *, takes: bool) -> None:
    assert guard.takes_value(word, guard.REVIEW_VALUE_FLAGS) is takes


def test_only_gh_itself_is_matched() -> None:
    assert guard.find("echo pr create x") is None
    assert guard.find("/usr/bin/gh pr create x") == guard.Match("create", ["x"])


@pytest.mark.parametrize(
    ("value", "name"), [("o/repoX", "o/repoX"), ("/o/r", "o/r"), ("h/o/r", "o/r")]
)
def test_repo_name_keeps_the_last_two_parts(value: str, name: str) -> None:
    assert guard.repo_name(value) == name


def test_a_failing_git_gives_no_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_args: str, cwd: Path | None = None) -> tuple[int, str]:
        del cwd
        return 1, "o/r\n"

    monkeypatch.setattr(guard, "git", failing)
    assert guard.origin_repo(None) is None


def test_a_long_flag_never_takes_a_short_flags_value() -> None:
    assert guard.takes_value("--xb", guard.REVIEW_VALUE_FLAGS) is False


def test_split_args_with_a_value_flag_last() -> None:
    assert guard.split_args(["7", "-b"], guard.REVIEW_VALUE_FLAGS) == (["-b"], ["7"])


def no_repo() -> str | None:
    return None


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        (["--help"], None),
        (
            ["-x", "pr", "review", "7", "-a", "-R", "o/r"],
            guard.Approval(A_REVIEW, "o/r#7"),
        ),
        (
            ["-x", "-R", "o/r", "pr", "review", "7", "-a"],
            guard.Approval(A_REVIEW, "o/r#7"),
        ),
        (
            ["pr", "review", "7", "-a", "--repo", "o/r"],
            guard.Approval(A_REVIEW, "o/r#7"),
        ),
    ],
)
def test_gh_approval_skips_global_flags(
    rest: list[str], expected: guard.Approval | None
) -> None:
    assert guard.gh_approval(rest, no_repo) == expected


def test_one_placeholder_takes_only_its_own_part_from_origin(tmp_path: Path) -> None:
    repo = origin_repo(tmp_path / "repo", "git@github.com:tbhb-dev/tools.git")
    text = "gh api repos/other/{repo}/pulls/7/reviews -f event=APPROVE"
    assert guard.approvals(text, repo) == [guard.Approval(AN_API, "other/tools#7")]
    text = "gh api repos/{owner}/x/pulls/7/reviews -f event=APPROVE"
    assert guard.approvals(text, repo) == [guard.Approval(AN_API, "tbhb-dev/x#7")]


def test_the_guard_checks_gh_approval_from_the_payloads_cwd(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repo = origin_repo(tmp_path / "repo", "git@github.com:o/r.git")
    (tmp_path / "plain").mkdir()
    monkeypatch.chdir(tmp_path / "plain")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    payload = {**CLAUDE_PAYLOAD, "cwd": str(repo)}
    assert guard_reason(monkeypatch, capsys, payload, "gh pr review 7 -a") == (
        GH_DENIED.format(route=A_REVIEW)
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gh pr review 7 -a -b -Rx/y -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh pr review 7 -a --body --repo=o/r -R tbhb-dev/tools", (A_REVIEW, TARGET)),
        ("gh pr review 7 -a -R tbhb-dev/tools -b -Ro/r", (A_REVIEW, TARGET)),
        ("gh pr review 7 -aR tbhb-dev/tools", (A_REVIEW, TARGET)),
        (
            "gh api repos/tbhb-dev/tools/pulls/7/reviews -f event=APPROVE -q -X GET",
            (AN_API, TARGET),
        ),
        (
            "gh api repos/o/r/pulls/7/reviews -f event=APPROVE -H --method=GET",
            (AN_API, "o/r#7"),
        ),
        (
            "gh api repos/tbhb-dev/tools/pulls/7/reviews -f body -f event=APPROVE",
            (AN_API, TARGET),
        ),
    ],
)
def test_a_flags_value_is_never_read_as_a_flag(
    text: str, expected: tuple[str, str | None]
) -> None:
    assert guard.approvals(text, None) == [guard.Approval(*expected)]


def test_a_value_word_that_looks_like_a_field_is_not_one() -> None:
    text = "gh api repos/tbhb-dev/tools/pulls/7/reviews -H -f event=APPROVE -X POST"
    assert guard.approvals(text, None) == []


def test_a_value_word_hides_the_repo_flag(tmp_path: Path) -> None:
    repo = origin_repo(tmp_path / "repo", "git@github.com:tbhb-dev/tools.git")
    text = "gh pr review 7 -a -b -Rx/y"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, TARGET)]


@pytest.mark.parametrize(
    ("words", "values"),
    [
        (["-X", "-f", "x"], ["-f"]),
        (["-f", "-X", "POST"], []),
        (["-H", "-Xv", "-Xw"], ["w"]),
        (["-aX", "GET"], ["GET"]),
        (["-aXv"], ["v"]),
        (["--method=", "x"], [""]),
        (["--method", "--x"], ["--x"]),
        (["--other", "-Xv"], ["v"]),
        (["x", "-", "-Xv"], ["v"]),
        (["-X"], []),
        (["aX", "v"], []),
    ],
)
def test_flag_values_skip_other_flags_values(
    words: list[str], values: list[str]
) -> None:
    flags = {"-X", "-f", "-H", "--method"}
    assert guard.flag_values(words, {"-X", "--method"}, flags) == values


@pytest.mark.parametrize(
    ("word", "found"),
    [
        ("--body", ("--body", None)),
        ("--body=", ("--body", "")),
        ("--body=x=y", ("--body", "x=y")),
        ("--body-x", None),
        ("-b", ("-b", None)),
        ("-bx", ("-b", "x")),
        ("-ab", ("-b", None)),
        ("-abR", ("-b", "R")),
        ("-a", None),
        ("-", None),
    ],
)
def test_flag_word(word: str, found: tuple[str, str | None] | None) -> None:
    assert guard.flag_word(word, guard.REVIEW_VALUE_FLAGS) == found


def test_gh_repo_names_the_repository(tmp_path: Path) -> None:
    repo = origin_repo(tmp_path / "repo", "git@github.com:o/r.git")
    assert guard.approvals("GH_REPO=tbhb-dev/tools gh pr review 7 -a", repo) == [
        guard.Approval(A_REVIEW, TARGET)
    ]
    text = "GH_REPO=x/y env GH_REPO=tbhb-dev/tools gh pr review 7 -a"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, TARGET)]
    text = "GH_REPO=tbhb-dev/tools bash -c 'gh pr review 7 -a'"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, TARGET)]
    text = "GH_REPO=x/y gh pr review 7 -a -R tbhb-dev/tools"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, TARGET)]
    text = "GH_REPO=tools gh pr review 7 -a"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, None)]
    text = (
        "GH_REPO=tbhb-dev/tools "
        "gh api repos/{owner}/{repo}/pulls/7/reviews -f event=APPROVE"
    )
    assert guard.approvals(text, repo) == [guard.Approval(AN_API, TARGET)]
    text = "gh pr review 7 -a GH_REPO=a/b"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, "o/r#7")]
    text = "GH_REPO_X=a/b gh pr review 7 -a"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, "o/r#7")]
    text = "export GH_REPO=a/b; gh pr review 7 -a"
    assert guard.approvals(text, repo) == [guard.Approval(A_REVIEW, "o/r#7")]


@pytest.mark.parametrize(
    "text",
    [
        "cd ../other && gh pr review 7 -a",
        "pushd ../other; gh pr review 7 -a",
        "popd; gh pr review 7 -a",
        "git -C ../other status; gh pr review 7 -a",
        "bash -c 'cd x' ; gh pr review 7 -a",
        "cd x; gh api repos/{owner}/{repo}/pulls/7/reviews -f event=APPROVE",
    ],
)
def test_a_directory_change_makes_the_origin_unknown(tmp_path: Path, text: str) -> None:
    repo = origin_repo(tmp_path / "repo", "git@github.com:tbhb-dev/tools.git")
    (found,) = guard.approvals(text, repo)
    assert found.target is None


@pytest.mark.parametrize(
    "text",
    [
        "gh pr review 7 -a; cd x",
        "git status; gh pr review 7 -a",
        "git commit -m x; gh pr review 7 -a",
        "cd x; gh pr review 7 -a -R tbhb-dev/tools",
        "cd x; GH_REPO=tbhb-dev/tools gh pr review 7 -a",
        "cd x; gh pr review https://github.com/tbhb-dev/tools/pull/7 -a",
    ],
)
def test_the_target_stays_known_without_a_move_or_with_a_named_repo(
    tmp_path: Path, text: str
) -> None:
    repo = origin_repo(tmp_path / "repo", "git@github.com:tbhb-dev/tools.git")
    (found,) = guard.approvals(text, repo)
    assert found.target == TARGET


PLACEHOLDERS = {
    "N": "7",
    "<N>": "7",
    "TEXT": "why",
    "completed|not-planned": "completed",
}


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create",
        "gh pr edit 3",
        "gh pr merge 3",
        "gh pr ready 3",
        "gh pr close 3",
        "gh issue close 3",
        "gh run rerun 9",
        "gh api -X DELETE repos/a/b/git/refs/heads/x",
    ],
)
def test_every_hinted_command_line_parses_with_the_real_cli(command: str) -> None:
    found = run_find(command)
    assert found is not None
    spans = re.findall(r"`tbhbdev alpha ([^`]+)`", guard.hint(found))
    assert spans
    for span in spans:
        words = [
            PLACEHOLDERS.get(w, w) for w in re.sub(r"\s*\[[^\]]*\]", "", span).split()
        ]
        if words[0] == "merge":
            squash_merge.build_parser("x").parse_args(words[1:])
        elif words[:2] in (["pr", "close"], ["issue", "close"]):
            close.build_parser("x").parse_args(words)
        else:
            pull_request.build_parser("x").parse_args(
                words[1:] if words[0] == "pr" else words
            )
