"""The coordinator keeps trust history even when a run host loses its home."""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.agent import driver, taint_store
from tbhbdev.alpha.agent.decisions import taint
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

HOST = "example-linux"
ACCOUNT = "agent"
RECORD = taint.Taint(
    HOST, ACCOUNT, "sample", "operator-chat-2026-11-10", "2026-11-10T12:00:00Z"
)
CLEARANCE = taint.Clearance(HOST, ACCOUNT, "Account rebuilt", "2026-11-10T13:00:00Z")


@pytest.fixture
def audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "coordinator"))
    return driver.directory() / "taint" / f"{HOST}.{ACCOUNT}.jsonl"


def test_append_clearance_and_later_taint(audit: Path) -> None:
    assert taint_store.load(HOST, ACCOUNT) == taint.State(HOST, ACCOUNT)
    assert not audit.parent.exists()
    taint_store.record_taint(RECORD)
    assert audit.parent == driver.directory() / "taint"
    assert stat.S_IMODE(audit.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    first = audit.read_bytes()
    assert json.loads(first) == {"kind": "taint", **asdict(RECORD)}
    assert taint_store.load(HOST, ACCOUNT).taint == RECORD
    taint_store.record_clearance(CLEARANCE)
    second = audit.read_bytes()
    assert second.startswith(first)
    assert json.loads(second.splitlines()[1]) == {
        "kind": "clearance",
        **asdict(CLEARANCE),
    }
    assert not taint_store.load(HOST, ACCOUNT).tainted
    later = replace(RECORD, run="later", started="2026-11-10T14:00:00Z")
    taint_store.record_taint(later)
    assert audit.read_bytes().startswith(second)
    assert len(audit.read_bytes().splitlines()) == 3
    assert taint_store.load(HOST, ACCOUNT).taint == later


def test_run_host_home_deletion_has_no_effect(audit: Path, tmp_path: Path) -> None:
    remote_home = tmp_path / "fake-host-home"
    remote_home.mkdir()
    (remote_home / "run.json").write_text("{}")
    taint_store.record_taint(RECORD)
    expected = audit.read_bytes()
    shutil.rmtree(remote_home)
    assert taint_store.load(HOST, ACCOUNT).taint == RECORD
    assert audit.read_bytes() == expected


@pytest.mark.parametrize(
    ("host", "account"), [("other-host", ACCOUNT), (HOST, "other")]
)
def test_store_identity_independence(audit: Path, host: str, account: str) -> None:
    taint_store.record_taint(RECORD)
    assert not taint_store.load(host, account).tainted
    other = replace(RECORD, host=host, account=account)
    taint_store.record_taint(other)
    taint_store.record_clearance(replace(CLEARANCE, host=host, account=account))
    assert not taint_store.load(host, account).tainted
    assert taint_store.load(HOST, ACCOUNT).taint == RECORD
    assert audit.exists()


@pytest.mark.parametrize(
    "line",
    [
        "{bad}\n",
        "\n",
        "null\n",
        "[]\n",
        "1\n",
        '"text"\n',
        '{"kind":"taint","host":1}\n',
        '{"kind":"taint","kind":"clearance"}\n',
        "{}\n",
        '{"kind":"taint"}\n',
        json.dumps({"kind": "taint", **asdict(RECORD)}),
        json.dumps({"kind": "taint", **asdict(RECORD), "extra": "field"}) + "\n",
        json.dumps({"kind": "other", **asdict(CLEARANCE)}) + "\n",
        json.dumps({"kind": "taint", **asdict(RECORD), "host": "other-host"}) + "\n",
        json.dumps({"kind": "taint", **asdict(RECORD), "account": "other"}) + "\n",
        json.dumps({"kind": "clearance", **asdict(CLEARANCE), "reason": ""}) + "\n",
        json.dumps({"kind": "taint", **asdict(RECORD), "started": "bad"}) + "\n",
    ],
)
def test_malformed_lines_fail_closed(audit: Path, line: str) -> None:
    taint_store.record_taint(RECORD)
    with audit.open("a") as stream:
        stream.write(line)
    loaded = taint_store.load(HOST, ACCOUNT)
    assert loaded.tainted
    assert loaded.problem.startswith("invalid local taint store: ")
    assert loaded.taint is None


def test_invalid_utf8_fails_closed(audit: Path) -> None:
    taint_store.record_taint(RECORD)
    with audit.open("ab") as stream:
        stream.write(b"\xff\n")
    assert "codec can't decode" in taint_store.load(HOST, ACCOUNT).problem


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("{}", "incomplete taint record line"),
        ("null\n", "taint record must be an object of strings"),
        ('{"kind":false}\n', "taint record must be an object of strings"),
        (
            json.dumps({"kind": "clearance", **asdict(CLEARANCE)})[:-1]
            + ',"reason":"different"}\n',
            "duplicate taint record field",
        ),
        (
            json.dumps({"kind": "other", **asdict(CLEARANCE)}) + "\n",
            "taint record has wrong kind, host or account",
        ),
    ],
)
def test_failure_message_identifies_reason(audit: Path, line: str, reason: str) -> None:
    taint_store.record_taint(RECORD)
    with audit.open("a") as stream:
        stream.write(line)
    assert taint_store.load(HOST, ACCOUNT) == taint.State(
        HOST, ACCOUNT, problem=f"invalid local taint store: {reason}"
    )


@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize("mode", [0o755, 0o777, 0o640, 0o1700])
def test_wrong_modes_fail_closed(audit: Path, level: int, mode: int) -> None:
    taint_store.record_taint(RECORD)
    target = (audit, audit.parent, audit.parent.parent)[level]
    target.chmod(mode)
    try:
        assert "wrong type or mode" in taint_store.load(HOST, ACCOUNT).problem
        with pytest.raises(Invalid, match="wrong type or mode"):
            taint_store.record_clearance(CLEARANCE)
    finally:
        target.chmod(0o700 if level else 0o600)


@pytest.mark.parametrize("level", [0, 1, 2, 3])
@pytest.mark.parametrize("dangling", [False, True])
def test_symlinks_fail_closed(audit: Path, level: int, *, dangling: bool) -> None:
    taint_store.record_taint(RECORD)
    target = (audit, *audit.parents)[level]
    moved = target.with_name(target.name + "-moved")
    target.rename(moved)
    destination = moved.with_name("missing") if dangling else moved
    target.symlink_to(destination, target_is_directory=level > 0)
    assert taint_store.load(HOST, ACCOUNT).tainted
    assert taint_store.load(HOST, ACCOUNT).problem
    with pytest.raises(OSError, match=r"Too many levels|Not a directory"):
        taint_store.record_clearance(CLEARANCE)


def test_wrong_ownership_fails_closed(
    audit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taint_store.record_taint(RECORD)
    uid = os.geteuid()
    monkeypatch.setattr(taint_store.os, "geteuid", lambda: uid + 1)
    assert taint_store.load(HOST, ACCOUNT).problem == (
        "invalid local taint store: taint store has wrong ownership"
    )
    with pytest.raises(Invalid, match="wrong ownership"):
        taint_store.record_taint(RECORD)
    assert len(audit.read_bytes().splitlines()) == 1


def test_file_type_must_be_regular(audit: Path) -> None:
    taint_store.record_taint(RECORD)
    audit.rename(audit.with_suffix(".saved"))
    audit.mkdir(mode=0o600)
    try:
        assert "wrong type or mode" in taint_store.load(HOST, ACCOUNT).problem
    finally:
        audit.chmod(0o700)


def test_short_write_is_error(audit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def short_write(_descriptor: int, _data: bytes) -> int:
        return 0

    monkeypatch.setattr(taint_store.os, "write", short_write)
    with pytest.raises(OSError, match=r"^incomplete taint store append$"):
        taint_store.record_taint(RECORD)
    assert audit.read_bytes() == b""


def test_empty_store_and_missing_file(audit: Path) -> None:
    taint_store.record_taint(RECORD)
    assert not taint_store.load(HOST, "missing").tainted
    audit.rename(audit.with_suffix(".saved"))
    audit.touch(mode=0o600)
    assert taint_store.load(HOST, ACCOUNT) == taint.State(HOST, ACCOUNT)


def test_bad_identity_never_reaches_disk(audit: Path) -> None:
    with pytest.raises(Invalid, match="invalid taint host or account"):
        taint_store.load("../bad", ACCOUNT)
    assert not audit.parent.exists()
