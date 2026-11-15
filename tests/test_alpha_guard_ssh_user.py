# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the ssh account guard."""

import io
import json
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha, userconfig
from tbhbdev.alpha import guard_ssh_user as guard

if TYPE_CHECKING:
    from pathlib import Path

NAMES = ["lab1", "lab2"]


@pytest.mark.parametrize(
    "command",
    [
        "ssh lab1",
        "ssh lab1 uptime",
        "git archive HEAD | ssh lab1 tar -x",
        "git archive HEAD | ssh -l tony lab1 tar -x",
        "ssh -v lab1",
        "ssh -qT lab1",
        "ssh tony@lab1",
        "ssh -l tony lab1",
        "ssh -l bob lab1",
        "ssh -o User=tony lab1",
        "ssh -oUser=tony lab1",
        "ssh -o 'User tony' lab1",
        "ssh -p 22 LAB1",
        "ssh -vl tony lab1",
        "ssh lab1.example",
        "ssh lab1.",
        "ssh ssh://lab1",
        "ssh ssh://tony@lab1:22",
        "ssh -i key -- lab1 uptime",
        "ssh -J lab1 other",
        "ssh -J agent@lab1,lab2 other",
        "ssh -o ProxyJump=lab1 other",
        "ssh -o 'ProxyJump lab1' other",
        "ssh -o ProxyJump=tony@lab1:22 other",
        "ssh -l agent -J lab1 other",
        "ssh -l tony -o User=agent lab1",
        "ssh [lab1] uptime",
        "/usr/bin/ssh lab1",
        "env FOO=1 ssh lab1",
        "bash -c 'ssh lab1'",
        "echo hi; ssh lab1",
        "true && ssh lab2 && true",
        "echo $(ssh lab1 hostname)",
        "scp file lab1:",
        "scp file lab1:/tmp",
        "scp tony@lab1:/etc/x .",
        "scp -o User=tony file lab1:",
        "scp -l 100 file lab1:",
        "scp scp://lab1/tmp/x .",
        "scp -J lab1 file other:",
        "scp -J tony@lab1 file other:",
        "scp 'file' [lab1]:/tmp",
        "rsync -a dir lab1:dst",
        "rsync -a lab1:src dir",
        "rsync -a tony@lab1:src dir",
        "rsync -e ssh dir lab1:dst",
        "rsync -e 'ssh -l tony' dir lab1:dst",
        "rsync -e'ssh' dir lab1:dst",
        "rsync --rsh='ssh -o User=tony' dir lab1:dst",
        'rsync --rsh="ssh -l \'unterminated" dir lab1:dst',
        "rsync -a rsync://lab1/mod dir",
        "rsync -a rsync://tony@lab1/mod dir",
    ],
)
def test_denied(command: str) -> None:
    reason = guard.verdict(command, NAMES)
    assert reason is not None
    assert "agent@" in reason
    assert "ops@" in reason


@pytest.mark.parametrize(
    "command",
    [
        "ssh agent@lab1",
        "git archive HEAD | ssh agent@lab1 tar -x",
        "ssh ops@lab1 uptime",
        "ssh -l agent lab1",
        "ssh -lops lab1",
        "ssh -o User=agent lab1",
        "ssh -oUser=ops lab1",
        "ssh -o 'user agent' lab1",
        "ssh -o StrictHostKeyChecking=no -l agent lab1",
        "ssh agent@lab1.example",
        "ssh ssh://agent@lab1:22",
        "ssh -o ProxyJump=agent@lab1 agent@lab2",
        "ssh -J ops@lab1 other",
        "ssh -l agent lab1 ssh tony@other",
        "ssh other",
        "ssh tony@other",
        "ssh lab9",
        "ssh lab10",
        "ssh prefix-lab1",
        "ssh",
        "ssh -",
        "ssh -p",
        "ssh -o ForwardAgent=no other",
        "ssh -o 'ProxyJump' other",
        "ssh -o",
        "ssh -J",
        "scp file agent@lab1:",
        "scp -o User=agent file lab1:",
        "scp -J agent@lab1 file other:",
        "scp file other:",
        "scp a b",
        "scp ./odd:name .",
        "scp",
        "rsync -a dir agent@lab1:dst",
        "rsync -a ops@lab1:src dir",
        "rsync -e 'ssh -l agent' dir lab1:dst",
        "rsync -e 'ssh -o User=ops' dir lab1:dst",
        "rsync -e 'ssh -l agent' dir lab2:dst",
        "rsync --rsh='ssh -l agent' dir lab1:dst",
        "rsync -a rsync://agent@lab1/mod dir",
        "rsync -a dir other:dst",
        "rsync -a dir dest",
        "rsync -a ./a:b dest",
        "rsync",
        "rsync -e",
        "ssh agent@lab2 ssh lab1",
        "ssh -o User=agent -l tony lab1",
        "git status",
        "echo ssh lab1",
        "echo 'ssh lab1'",
        "cat ssh",
        "mosh lab1",
        "sftp lab1",
        "",
    ],
)
def test_allowed(command: str) -> None:
    assert guard.verdict(command, NAMES) is None


def test_the_reason_names_the_host_as_typed() -> None:
    reason = guard.verdict("ssh LAB1.example", NAMES)
    assert reason is not None
    assert "agent@LAB1.example" in reason


def test_no_names_allows_everything() -> None:
    assert guard.verdict("ssh lab1", []) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tony@h", ("tony", "h")),
        ("h", (None, "h")),
        ("h:22", (None, "h")),
        ("ssh://u@h:22/x", ("u", "h")),
        ("ssh://[::1]", (None, "::1")),
        ("[::1]", (None, "::1")),
    ],
)
def test_split_target(text: str, expected: tuple[str | None, str]) -> None:
    assert guard.split_target(text) == expected


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("User=a", "a"),
        ("user a", "a"),
        ("User=", None),
        ("Port=1", None),
        ("User", None),
    ],
)
def test_option_user(option: str, expected: str | None) -> None:
    assert guard.option_user(option) == expected


def hook(
    command: str, *, agy: bool, monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> None:
    monkeypatch.setattr(guard, "hosts", lambda: names)
    if agy:
        payload = {"toolCall": {"args": {"CommandLine": command}}}
    else:
        payload = {"tool_input": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


@pytest.mark.parametrize("agy", [False, True])
def test_hook_contract(
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook("ssh lab1", agy=agy, monkeypatch=monkeypatch, names=NAMES)
    assert alpha.main(["guard-ssh-user"], "tbhbdev alpha") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    reason = guard.REASON.format(host="lab1")
    assert json.loads(captured.out) == guard.decision(reason, antigravity=agy)


@pytest.mark.parametrize(
    ("command", "names"),
    [("ssh agent@lab1", NAMES), ("ssh lab1", []), ("git status", NAMES)],
)
def test_hook_allows_silently(
    command: str,
    names: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook(command, agy=False, monkeypatch=monkeypatch, names=names)
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", ["", "{bad", "[]", "{}"])
def test_bad_stdin_passes(
    payload: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(guard, "hosts", lambda: NAMES)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_a_crash_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash() -> list[str]:
        raise RuntimeError

    hook("ssh lab1", agy=False, monkeypatch=monkeypatch, names=NAMES)
    monkeypatch.setattr(guard, "hosts", crash)
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def write_config(path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(userconfig, "path", lambda: path)


def test_hosts_come_from_the_harness_table_in_lowercase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(
        tmp_path / "config.toml",
        '[harness]\nagent_user_hosts = ["Lab1", "lab2"]\n',
        monkeypatch,
    )
    assert guard.hosts() == ["lab1", "lab2"]


@pytest.mark.parametrize("body", ["", "[harness]\nhosts = []\n"])
def test_no_key_means_no_hosts(
    body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path / "config.toml", body, monkeypatch)
    assert guard.hosts() == []


def test_a_broken_config_fails_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_config(
        tmp_path / "config.toml", "[harness]\nagent_user_hosts = [1]\n", monkeypatch
    )
    payload = {"tool_input": {"command": "ssh lab1"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")
