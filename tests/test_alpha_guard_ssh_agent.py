# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the SSH agent guard."""

import io
import json
import sys
from pathlib import Path

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import guard_ssh_agent as guard

S = ".ssh"
KEY = "-i /k -o IdentitiesOnly=yes"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("ssh host true", guard.REASON_AGENT),
        ("scp a host:b", guard.REASON_AGENT),
        ("sftp host", guard.REASON_AGENT),
        ("ssh -i /k host", guard.REASON_AGENT),
        ("ssh -o IdentitiesOnly=yes host", guard.REASON_AGENT),
        ("bash -c 'ssh host'", guard.REASON_AGENT),
        ("rsync -a a host:b", guard.REASON_AGENT),
        ("rsync -e ssh -a a b", guard.REASON_AGENT),
        ("ssh-add -l", guard.REASON_ADD),
        (f"cat ~/{S}/config", guard.REASON_DIR),
        (f"ls $HOME/{S}", guard.REASON_DIR),
        (f"cp ${{HOME}}/{S}/id_x /tmp", guard.REASON_DIR),
        (f"x=~/{S}/id cat", guard.REASON_DIR),
        ("SSH_AUTH_SOCK=/s ssh -o IdentityAgent=none h", guard.REASON_SOCK),
        ("export SSH_AUTH_SOCK=/s", guard.REASON_SOCK),
    ],
)
def test_denied(command: str, reason: str) -> None:
    assert guard.verdict(command) == reason


@pytest.mark.parametrize(
    "command",
    [
        "ssh -o IdentityAgent=none host true",
        "ssh -oIdentityAgent=none host",
        "ssh -o 'IdentityAgent none' host",
        f"ssh {KEY} host",
        "ssh -i/k -o IdentitiesOnly=yes host",
        "scp -o IdentityAgent=none a host:b",
        "rsync -a -e 'ssh -o IdentityAgent=none' a host:b",
        "rsync -a a b",
        "rsync -a ./a:b c",
        f"ls ~/{S}/sessions",
        f"ls ~/{S}/sessions/x",
        f"ls ~/{S}x",
        f"ls ~/{S}-other",
        "env -u SSH_AUTH_SOCK git status",
        "unset SSH_AUTH_SOCK",
        "SSH_AUTH_SOCK= git status",
        "echo ssh",
        "git status",
        "",
    ],
)
def test_allowed(command: str) -> None:
    assert guard.verdict(command) is None


def test_the_literal_home_path_is_denied() -> None:
    assert guard.verdict(f"cat {Path.home()}/{S}/config") == guard.REASON_DIR


@pytest.mark.parametrize("agy", [False, True])
def test_hook_contract(
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if agy:
        stdin = json.dumps({"toolCall": {"args": {"CommandLine": "ssh-add"}}})
    else:
        stdin = json.dumps({"tool_input": {"command": "ssh-add"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert alpha.main(["guard-ssh-agent"], "tbhbdev alpha") == 0
    out = json.loads(capsys.readouterr().out)
    if agy:
        assert out == {"decision": "deny", "reason": guard.REASON_ADD}
    else:
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allow_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"command": "git status"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    assert capsys.readouterr().out == ""


def test_a_crash_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_text: str) -> str:
        raise RuntimeError

    monkeypatch.setattr(guard, "verdict", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert guard.main() == 0
    assert capsys.readouterr().out == ""
