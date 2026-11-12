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


@pytest.mark.parametrize("payload", ["", "{bad", "[]", "{}"])
def test_bad_stdin_passes(
    payload: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_hook_crashes_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash() -> None:
        raise RuntimeError

    monkeypatch.setattr(guard, "read_hook_stdin", crash)
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")
