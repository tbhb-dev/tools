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
    "git config core.hooksPath list",
    "git -C repo commit --no-verify -m x",
    "git --config-env=core.hooksPath=VAR push",
    'git -c "core.hooksPath = /tmp/h" push',
    "bash -c 'GIT_CONFIG_PARAMETERS=\"core.hooksPath=/x\" git push'",
    "git commit -S --no-verify -m x",
    "git commit -nm message",
    "git commit -anm message",
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
    "git log --grep='GIT_CONFIG_' --grep='core.hooksPath' -1",
    "git log -n 5 --grep commit",
    "git log --grep config --grep core.hooksPath HEAD",
    "git log --author '--no-verify'",
    "git -c 'alias.hookspath=config --get core.hooksPath' log",
    "git -C repo log --version",
    "git --no-pager log",
    "GIT_CONFIG_NOSYSTEM=1 git config --get core.hooksPath",
    'git commit -m "docs: \\"GIT_CONFIG_PARAMETERS=...\\" overrides core.hooksPath"',
    "git log --grep '-c\x63ore.hooksPath=/dev/null'",
    "git commit -m 'docs: GIT_CONFIG_PARAMETERS= overrides core.hooksPath'",
    "git config -t path core.hooksPath",
    "git config unset core.hooksPath",
    "git -c\x75ser.name=x log",
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


PUSH_DENIED = [
    "git push --force",
    "git push -f origin topic",
    "git push origin topic -f",
    "git push -fu origin topic",
    "git push -uf origin topic",
    "git push --forc origin topic",
    "git push --force=x",
    "git push --mirror",
    "git push --mir origin",
    "git push origin +topic",
    "git push origin +HEAD:topic",
    "git push origin -- +topic",
    "git push --force-if-includes origin topic",
    "git push --force-with-lease --force origin topic",
    "git push --force-with-lease -f origin topic",
    "git push --force-with-lease --force-with origin topic",
    "git push --force-with-lease origin HEAD:main",
    "git push --force-with-lease origin HEAD:refs/heads/master",
    "git push origin main",
    "git push origin topic main",
    "git push origin :main",
    "git push --delete origin main",
    "git push -o ci.skip -f origin topic",
    "git -C repo push --force origin topic",
    "cd x && git push -f",
    "bash -c 'git push --force-if-includes'",
]
PUSH_ALLOWED = [
    "git push",
    "git push origin topic",
    "git push -u origin topic",
    "git push --force-with-lease",
    "git push --force-with-lease origin topic",
    "git push --force-with-lease=topic origin topic",
    "git push --force-with-lease=topic:abc123 origin topic",
    "git push --force-with-lease --force-if-includes origin topic",
    "git push --force-if-includes --force-with-lease=topic origin topic",
    "git push -o force origin topic",
    "git push -ofoo origin topic",
    "git push --push-option -f origin topic",
    "git push --repo -f HEAD",
    "git push --follow-tags",
    "git push -n origin topic",
    "git push origin topic:feature/main",
    "git push origin HEAD:refs/heads/topic",
    "git push main topic",
    "git push -- origin topic",
    "git log --force -f",
    "git fetch --force origin",
    "echo git push --force",
]


@pytest.mark.parametrize("command", PUSH_DENIED)
def test_push_denied(command: str) -> None:
    assert guard.verdict(command) == guard.PUSH_REASON


@pytest.mark.parametrize("command", PUSH_ALLOWED)
def test_push_allowed(command: str) -> None:
    assert guard.verdict(command) is None


def test_poll_hook_denies_force_push(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"command": "git push --force origin topic"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert poll.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == guard.PUSH_REASON
