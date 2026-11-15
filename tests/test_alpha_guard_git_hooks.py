# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the git hook bypass guard."""

import io
import json
import sys

import pytest

from tbhbdev.alpha import guard_gh_poll as poll
from tbhbdev.alpha import guard_git_hooks as guard

DENIED = [
    "git -c core.hooksPath=/tmp/h push origin x",
    "git -c core.hookspath=/dev/null push",
    "git --config-env=core.hooksPath=HOOKS push",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/tmp/h'\" git push",
    "git config core.hooksPath /tmp/h",
    "git config --global core.hooksPath /tmp/h",
    "git commit --no-verify -m x",
    "git push --no-verify",
    "true && git -c core.hooksPath=/x push",
    "bash -c 'git -c core.hooksPath=/x push'",
]
ALLOWED = [
    "git push origin new-branch",
    "git config --show-origin --get core.hooksPath",
    "git config --get-all core.hooksPath",
    "git commit -m 'document --no-verify'",
    "git log -1",
    "echo core.hooksPath",
    "ls .git/hooks",
]


@pytest.mark.parametrize("command", DENIED)
def test_denied(command: str) -> None:
    assert guard.verdict(command) == guard.REASON


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command: str) -> None:
    assert guard.verdict(command) is None


def test_poll_hook_denies_bypass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"command": DENIED[0]}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert poll.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
