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


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("ssh-add -l", guard.REASON_ADD),
        ("/usr/bin/ssh-add", guard.REASON_ADD),
        ("true && ssh-add", guard.REASON_ADD),
        ("sudo -u deploy ssh-add", guard.REASON_ADD),
        ("env -- FOO=1 ssh-add", guard.REASON_ADD),
        ("FOO=1 command ssh-add", guard.REASON_ADD),
        (f"cat ~/{S}/config", guard.REASON_DIR),
        (f"wc -c <~/{S}/config", guard.REASON_DIR),
        (f"ls $HOME/{S}", guard.REASON_DIR),
        (f"cp ${{HOME}}/{S}/id_x /tmp", guard.REASON_DIR),
        (f"x=~/{S}/id cat", guard.REASON_DIR),
        (f"sudo cat ~/{S}/id", guard.REASON_DIR),
    ],
)
def test_denied(command: str, reason: str) -> None:
    assert guard.verdict(command) == reason


@pytest.mark.parametrize(
    "command",
    [
        "ssh host true",
        "scp a host:b",
        "rsync -a a host:b",
        "ssh -V",
        f"ls ~/{S}/sessions",
        f"ls ~/{S}/sessions/x",
        f"ls ~/{S}x",
        f"ls ~/{S}-other",
        f"echo ~/{S}/config",
        f"printf '%s' ~/{S}/config",
        f"git commit -m 'read config=~/{S}/config'",
        "SSH_AUTH_SOCK=/s git status",
        "nohup git status",
        "env FOO=1",
        "echo ssh-add",
        "git status",
        "echo 'unbalanced",
        "",
    ],
)
def test_allowed(command: str) -> None:
    assert guard.verdict(command) is None


def test_the_literal_home_path_is_denied() -> None:
    assert guard.verdict(f"cat {Path.home()}/{S}/config") == guard.REASON_DIR


def test_a_relative_ssh_path_counts_only_from_the_home_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert guard.verdict(f"cat {S}/config") is None
    monkeypatch.chdir(Path.home())
    assert guard.verdict(f"cat {S}/config") == guard.REASON_DIR
    assert guard.verdict(f"cat {S}/sessions/k") is None


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


@pytest.mark.parametrize(
    ("tool_input", "denied"),
    [
        ({"file_path": f"{Path.home()}/{S}/id_x"}, True),
        ({"path": f"~/{S}/config"}, True),
        ({"file_path": f"{Path.home()}/{S}/sessions/k"}, False),
        ({"file_path": "/etc/hosts"}, False),
        ({"file_path": 3}, False),
    ],
)
def test_a_tool_path_is_checked(
    *,
    tool_input: dict[str, object],
    denied: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"tool_input": tool_input}))
    )
    assert guard.main() == 0
    assert bool(capsys.readouterr().out) is denied


def test_a_payload_without_tool_input_is_allowed() -> None:
    assert guard.path_reason({"tool_input": "x"}) is None


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
