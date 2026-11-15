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
    "git commit -n -m x",
    "export HOOKS=core.hooksPath",
    "git --config-env core.hooksPath=VAR push",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/x'\"; export GIT_CONFIG_PARAMETERS",
    "export GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_COUNT=1; git push",
    "git commit -an -m x",
    "git push --no-ver",
    "export GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/dev/null'\"; git push",
    "git -c\x63ore.hooksPath=/x push",
]
ALLOWED = [
    "git push origin new-branch",
    "git config --show-origin --get core.hooksPath",
    "git config --get-all core.hooksPath",
    "git commit -m 'document --no-verify'",
    "git log -1",
    "echo core.hooksPath",
    "ls .git/hooks",
    "git config core.hooksPath",
    "git config --file .git/config core.hooksPath",
    "git config -f .git/config core.hooksPath",
    "git config get core.hooksPath",
    "git log --grep '--no-verify'",
    "git commit -m --no-verify",
    "git commit -uno -m x",
    "git config --unset core.hooksPath",
    "git log --grep=core.hooksPath",
    "git commit -m core.hooksPath",
    "git push -n origin x",
    "git push --no-thin",
    "git -c user.name=x log",
    "git -c",
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
