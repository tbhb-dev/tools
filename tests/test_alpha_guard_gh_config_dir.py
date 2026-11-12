# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the gh configuration environment guard."""

import io
import json
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import guard_gh_config_dir as guard

if TYPE_CHECKING:
    from conftest import Stubs


@pytest.mark.parametrize("value", [None, "", "/nonexistent/gh-config", " "])
@pytest.mark.parametrize(
    "command",
    [
        "gh",
        "gh --version",
        "gh auth status",
        "gh api user | cat",
        "printf x | gh api user",
        "true && gh pr view 1",
        "/usr/bin/gh issue list",
        "env gh api user",
        "bash -c 'gh api user'",
        "echo $(gh api user)",
    ],
)
@pytest.mark.parametrize("agy", [False, True])
def test_hook_contract(
    value: str | None,
    command: str,
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if value is None:
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("GH_CONFIG_DIR", value)
    if agy:
        stdin = json.dumps({"toolCall": {"args": {"CommandLine": command}}})
    else:
        stdin = json.dumps({"tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert alpha.main(["guard-gh-config-dir"], "tbhbdev alpha") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if value:
        assert captured.out == ""
    elif agy:
        assert json.loads(captured.out) == {"decision": "deny", "reason": guard.REASON}
    else:
        assert json.loads(captured.out) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": guard.REASON,
            }
        }


@pytest.mark.parametrize(
    "command", ["git status", "echo gh", "echo 'gh api user'", "", "gh-other"]
)
def test_non_gh_commands_pass(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    payload = {"tool_input": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_reason_names_the_variable() -> None:
    assert "GH_CONFIG_DIR" in guard.REASON


@pytest.mark.parametrize("value", ["", "/nonexistent/gh-config"])
def test_hook_never_runs_gh(
    value: str,
    stubs: Stubs,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GH_CONFIG_DIR", value)
    payload = {"tool_input": {"command": "gh auth status"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    assert bool(capsys.readouterr().out) is (not value)
    assert stubs.calls() == []


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{bad",
        "[]",
        "{}",
        '{"tool_input": {}}',
        '{"tool_input": {"command": null}}',
        '{"tool_input": {"command": 123}}',
        '{"tool_input": "true"}',
        '{"toolCall": {}}',
        '{"toolCall": {"args": []}}',
        '{"toolCall": {"args": {"CommandLine": false}}}',
        '{"toolCall": {}, "tool_input": {"command": "true"}}',
    ],
)
def test_bad_stdin_is_denied(
    payload: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == (
        json.dumps(
            guard.decision(guard.REASON_INPUT, antigravity="toolCall" in payload)
        )
        + "\n",
        "",
    )


@pytest.mark.parametrize("agy", [False, True])
def test_hook_crashes_fail_closed(
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def crash(_text: str) -> None:
        raise RuntimeError

    payload = (
        {"toolCall": {"args": {"CommandLine": "true"}}}
        if agy
        else {"tool_input": {"command": "true"}}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(guard, "verdict", crash)
    assert guard.main() == 0
    assert capsys.readouterr() == (
        json.dumps(guard.decision(guard.REASON_CRASH, antigravity=agy)) + "\n",
        "",
    )


def test_unreadable_stdin_is_denied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin = io.StringIO()
    stdin.close()
    monkeypatch.setattr(sys, "stdin", stdin)
    assert guard.main() == 0
    assert capsys.readouterr() == (
        json.dumps(guard.decision(guard.REASON_INPUT, antigravity=False)) + "\n",
        "",
    )


OWN = "/home/agent/.config/gh-agent"


def run(command: str, own: str | None, monkeypatch: pytest.MonkeyPatch) -> str | None:
    """The deny reason the hook gives for ``command``, or None when it allows."""
    monkeypatch.setenv("HOME", "/home/agent")
    if own is None:
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("GH_CONFIG_DIR", own)
    return guard.verdict(command)


@pytest.mark.parametrize("own", [None, "", OWN])
@pytest.mark.parametrize(
    ("dynamic", "literal"),
    [
        ("source script.sh", "echo source script.sh"),
        (". script.sh", "echo . script.sh"),
        ("eval '$SCRIPT'", "eval 'true'"),
        ('eval "$SCRIPT"', 'eval "true"'),
        ("eval $SCRIPT", "eval true"),
        ("eval true '$SCRIPT'", "eval true true"),
        ("eval '`printf true`'", "eval 'printf true'"),
        ('eval "`printf true`"', 'eval "printf true"'),
        ("eval `printf true`", "eval printf true"),
        ("eval '$(printf true)'", "eval 'printf true'"),
        ('eval "$(printf true)"', 'eval "printf true"'),
        ("eval $(printf true)", "eval printf true"),
        ("eval echo$(printf true)", "eval echo"),
        ('name=GH_CONFIG_DIR; eval "unset $name"', 'name=OTHER; eval "unset OTHER"'),
        ("bash -c 'source script.sh'", "bash -c 'echo source script.sh'"),
        ("eval '. script.sh'", "eval 'echo . script.sh'"),
        ("bash -c 'eval $SCRIPT'", "bash -c 'eval true'"),
    ],
)
def test_unreadable_shell_text_is_denied_but_literal_commands_pass(
    dynamic: str,
    literal: str,
    own: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run(dynamic, own, monkeypatch) == guard.REASON_SHELL_TEXT
    assert run(literal, own, monkeypatch) is None


@pytest.mark.parametrize("own", [None, "", OWN])
@pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "dash", "ksh", "/bin/bash"])
@pytest.mark.parametrize(
    "script",
    [
        "$SCRIPT",
        '"$SCRIPT"',
        "'$SCRIPT'",
        "'`printf true`'",
        '"`printf true`"',
        "`printf true`",
        "'$(printf true)'",
        '"$(printf true)"',
        "$(printf true)",
        '"echo $(printf true)"',
    ],
)
def test_dynamic_shell_scripts_are_denied_but_literal_scripts_pass(
    shell: str,
    script: str,
    own: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run(f"{shell} -c {script}", own, monkeypatch) == guard.REASON_SHELL_TEXT
    assert run(f"{shell} -c 'true'", own, monkeypatch) is None


@pytest.mark.parametrize(
    "command",
    [
        "(true)",
        "bash",
        "bash -c",
        "bash script.sh",
        "bash -c 'true' '$SCRIPT'",
        "eval",
        "echo '$SCRIPT'",
        "echo $(printf true)",
        "echo `printf true`",
        "command eval 'true'",
        "bash -c 'eval true'",
        "bash -c 'bash -c true'",
    ],
)
def test_literal_shell_text_and_other_arguments_pass(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(command, None, monkeypatch) is None


@pytest.mark.parametrize("agy", [False, True])
def test_dynamic_shell_text_prints_a_deny_decision(
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = 'eval "$SCRIPT"'
    payload = (
        {"toolCall": {"args": {"CommandLine": command}}}
        if agy
        else {"tool_input": {"command": command}}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    assert capsys.readouterr() == (
        json.dumps(guard.decision(guard.REASON_SHELL_TEXT, antigravity=agy)) + "\n",
        "",
    )


@pytest.mark.parametrize(
    "command",
    [
        "GH_CONFIG_DIR= gh api user",
        'GH_CONFIG_DIR="" gh api user',
        "env -u GH_CONFIG_DIR gh api user",
        "env --unset GH_CONFIG_DIR gh api user",
        "env --unset=GH_CONFIG_DIR gh api user",
        "unset GH_CONFIG_DIR; gh api user",
        "GH_CONFIG_DIR=$HOME/.config/gh gh api user",  # gitleaks:allow
        "GH_CONFIG_DIR=~/.config/gh gh api user",  # gitleaks:allow
        "export GH_CONFIG_DIR=/home/agent/.config/gh; gh api user",  # gitleaks:allow
        "export GH_CONFIG_DIR; gh api user",
        "declare -x GH_CONFIG_DIR=/elsewhere; gh api user",
        "env GH_CONFIG_DIR=/elsewhere gh api user",
        "env -i gh api user",
        "env - gh api user",
        "bash -c 'GH_CONFIG_DIR= gh api user'",
        "GH_CONFIG_DIR=/elsewhere true",
        "GH_CONFIG_DIR=$HOME/.config/gh; gh api user",  # gitleaks:allow
        "GH_CONFIG_DIR=$HOME/.config/gh && gh api user",  # gitleaks:allow
        "GH_CONFIG_DIR=$HOME/.config/gh\ngh api user",  # gitleaks:allow
        "GH_CONFIG_DIR=; gh api user",
        "GH_CONFIG_DIR+=/../../../x/.config/gh gh api user",  # gitleaks:allow
        "GH_CONFIG_DIR+=/../x; gh api user",
        "FOO=1 GH_CONFIG_DIR+=/x gh api user",
        "env GH_CONFIG_DIR+=/x gh api user",
        "if true; then GH_CONFIG_DIR=/x; fi; gh api user",
        "bash -c 'GH_CONFIG_DIR=/x; gh api user'",
        "eval 'GH_CONFIG_DIR=/x; gh api user'",
    ],
)
def test_changing_the_variable_is_denied(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(command, OWN, monkeypatch) == guard.REASON_OVERRIDE


@pytest.mark.parametrize(
    "command",
    [
        "GH_CONFIG_DIR=$HOME/.config/gh-agent gh api user",  # gitleaks:allow
        "GH_CONFIG_DIR=/home/agent/.config/gh-agent/ gh api user",
        "GH_CONFIG_DIR=~/.config/gh-agent gh api user",  # gitleaks:allow
        "GH_CONFIG_DIR=$GH_CONFIG_DIR gh api user",
        "GH_CONFIG_DIR=${GH_CONFIG_DIR} gh api user",
        "export GH_CONFIG_DIR=$HOME/.config/gh-agent; gh api user",  # gitleaks:allow
        "echo $GH_CONFIG_DIR",
        "grep GH_CONFIG_DIR README.md",
        "grep GH_CONFIG_DIR=x README.md",
        "env FOO=1 gh api user",
        "env --unset=PATH true",
        "FOO=1; gh api user",
        "GH_CONFIG_DIR=$GH_CONFIG_DIR; gh api user",
        "bash -c 'FOO=1; true'",
    ],
)
def test_the_agent_config_dir_and_mentions_pass(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(command, OWN, monkeypatch) is None


def test_a_gh_call_without_the_variable_in_the_hook_environment_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run("gh api user", None, monkeypatch) == guard.REASON
    assert run("GH_CONFIG_DIR=/x gh api user", None, monkeypatch) == (
        guard.REASON_OVERRIDE
    )
