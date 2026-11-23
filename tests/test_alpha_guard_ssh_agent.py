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
SAFE = "-i /k -o IdentityAgent=none"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("ssh host true", guard.REASON_AGENT),
        ("scp a host:b", guard.REASON_AGENT),
        ("sftp host", guard.REASON_AGENT),
        ("ssh -i /k host", guard.REASON_AGENT),
        ("ssh -o IdentityAgent=none host", guard.REASON_AGENT),
        ("ssh -i /k -o IdentitiesOnly=yes host", guard.REASON_AGENT),
        (
            "ssh -o IdentityAgent=/sock -i /k -o IdentityAgent=none host",
            guard.REASON_AGENT,
        ),
        ("ssh -vi /k host", guard.REASON_AGENT),
        ("ssh host -o IdentityAgent=none -i /k", guard.REASON_AGENT),
        ("ssh -- host", guard.REASON_AGENT),
        ("ssh -v host", guard.REASON_AGENT),
        ("rsync host:b -e", guard.REASON_AGENT),
        ("ssh -", guard.REASON_AGENT),
        ("bash -c 'ssh host'", guard.REASON_AGENT),
        ("env -u LANG ssh host", guard.REASON_AGENT),
        ("sudo -u deploy ssh host", guard.REASON_AGENT),
        ("env -- ssh host", guard.REASON_AGENT),
        ("FOO=1 /usr/bin/ssh host", guard.REASON_AGENT),
        ("true && ssh host", guard.REASON_AGENT),
        ("x=$(ssh host uptime)", guard.REASON_AGENT),
        ('echo "$(ssh host uptime)"', guard.REASON_AGENT),
        ("echo `ssh host`", guard.REASON_AGENT),
        ("cat <<EOF\n$(ssh host uptime)\nEOF", guard.REASON_AGENT),
        ("rsync -a a host:b", guard.REASON_AGENT),
        ("rsync -e ssh -a a host:b", guard.REASON_AGENT),
        ("rsync -avze 'ssh -i /k' a host:b", guard.REASON_AGENT),
        ("rsync --rsh='ssh -i /k' a host:b", guard.REASON_AGENT),
        ("rsync --rsh 'ssh -i /k' a host:b", guard.REASON_AGENT),
        ("rsync -essh a host:b", guard.REASON_AGENT),
        ("ssh-add -l", guard.REASON_ADD),
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
        f"ssh {SAFE} host true",
        f"ssh {SAFE} host 'ssh other'",
        "ssh -oIdentityAgent=none -i/k host",
        "ssh -o 'IdentityAgent none' -i /k host",
        "ssh -o 'IdentityAgent=\"none\"' -i /k host",
        'ssh -i "$(key_path)" -o IdentityAgent=none host',
        f"scp {SAFE} a host:b",
        f"sftp {SAFE} host",
        f"rsync -a -e 'ssh {SAFE}' a host:b",
        f"rsync -a --rsh='ssh {SAFE}' a host:b",
        f"RSYNC_RSH='ssh {SAFE}' rsync -a a host:b",
        "rsync -a -e 'ssh -i /k' a b",
        "rsync -a -e 'rsh' a host:b",
        "rsync -e '' a host:b",
        "rsync -a a b",
        "rsync -a ./a:b c",
        "rsync -a rsync://host/mod/x b",
        "rsync -a host::mod b",
        f"ls ~/{S}/sessions",
        f"ls ~/{S}/sessions/x",
        f"ls ~/{S}x",
        f"ls ~/{S}-other",
        f"echo ~/{S}/config",
        f"printf '%s' ~/{S}/config",
        f"scp {SAFE} host:~/{S}/config .",
        "git commit -m 'SSH_AUTH_SOCK=/tmp/session.sock'",
        "SSH_AUTH_SOCK=/s git status",
        "export SSH_AUTH_SOCK=/s",
        "command -v ssh",
        "command -V ssh",
        "nohup git status",
        "env -u SSH_AUTH_SOCK git status",
        "echo ssh",
        "bash -c",
        "git status",
        "echo 'unbalanced",
        "",
    ],
)
def test_allowed(command: str) -> None:
    assert guard.verdict(command) is None


def test_command_run_through_a_wrapper_is_still_checked() -> None:
    assert guard.verdict("command ssh host") == guard.REASON_AGENT


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
