"""Private atomic records and writers waiting on the index's stable lock."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_host_example import bounded_file_reads
from test_alpha_agent_request import BUILD

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Generator


def test_private_request_and_snapshot(tmp_path: Path) -> None:
    files = runfiles.RunFiles(tmp_path / "runs", "run-1")
    assert files.name == "run-1"
    assert files.root == tmp_path / "runs"
    assert files.root.stat().st_mode & 0o777 == 0o700
    assert files.snapshot() == {}
    files.receive(request.parse(BUILD, host_allows_unsandboxed=False))
    assert "run-1.request.json" in {p.name for p in files.root.iterdir()}
    assert json.loads(files.path("request.json").read_text()) == BUILD
    assert files.path("request.json").stat().st_mode & 0o777 == 0o600
    for suffix, text in (("phase", "running"), ("launched", ""), ("rc", "0\n")):
        files.write(suffix, text)
    assert files.snapshot() == {"phase": "running", "launched": "", "rc": "0\n"}
    files.root.chmod(0o755)
    assert runfiles.private_dir(files.root).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name", ["", "../x", "x/y", "-x", "x" * 65])
def test_invalid_run_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(Invalid) as caught:
        runfiles.RunFiles(tmp_path, name)
    assert (
        str(caught.value)
        == f"run name {name!r} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}"
    )


def test_rc_is_replaced_only_after_complete_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "run.rc"
    target.write_text("old\n")
    replace = Path.replace
    events: list[str] = []
    sync = os.fsync

    def synced(fd: int) -> None:
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        assert target.read_bytes() == (b"124\n" if directory else b"old\n")
        events.append("directory-sync" if directory else "sync")
        sync(fd)

    def replaced(path: Path, destination: Path) -> Path:
        assert target.read_text() == "old\n"
        assert path.parent == tmp_path
        assert path.read_text() == "124\n"
        assert path.stat().st_mode & 0o777 == 0o600
        events.append("replace")
        return replace(path, destination)

    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(Path, "replace", replaced)
    runfiles.atomic(target, "124\n")
    assert target.read_text() == "124\n"
    assert target.stat().st_mode & 0o777 == 0o600
    assert events == ["sync", "replace", "directory-sync"]


def test_index_keeps_last_line(tmp_path: Path) -> None:
    assert runfiles.latest(tmp_path) == {}
    lines: list[dict[str, object]] = [
        {"v": 1, "run": "a", "state": "running", "trace_id": None},
        {"v": 1, "run": "b", "state": "running", "trace_id": None},
        {"v": 1, "run": "a", "state": "ended", "trace_id": None},
    ]
    for line in lines:
        runfiles.append(tmp_path, line)
    assert runfiles.latest(tmp_path) == {"a": lines[2], "b": lines[1]}
    assert [
        json.loads(line) for line in (tmp_path / "index.jsonl").read_text().splitlines()
    ] == lines
    assert (tmp_path / "index.jsonl").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "index.lock").stat().st_mode & 0o777 == 0o600


def test_waiting_append_opens_index_after_lock(tmp_path: Path) -> None:
    runfiles.append(tmp_path, {"run": "old"})
    code = """\
import sys
from pathlib import Path
from tbhbdev.alpha.agent import runfiles
print('ready', flush=True)
runfiles.append(Path(sys.argv[1]), {'run': 'new'})
"""
    with (tmp_path / "index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        inode = os.fstat(lock.fileno()).st_ino
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline() == "ready\n"
            replacement = tmp_path / "replacement"
            replacement.write_text('{"run":"replacement"}\n')
            replacement.replace(tmp_path / "index.jsonl")
            fcntl.flock(lock, fcntl.LOCK_UN)
            assert child.wait(timeout=10) == 0
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
    assert (tmp_path / "index.lock").stat().st_ino == inode
    assert runfiles.latest(tmp_path) == {
        "replacement": {"run": "replacement"},
        "new": {"run": "new"},
    }


def test_private_directory_mode_at_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "nested" / "runs"
    chmod = Path.chmod

    def checked(path: Path, mode: int) -> None:
        assert path == root
        assert path.stat().st_mode & 0o777 == 0o700
        chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", checked)
    previous = os.umask(0)
    try:
        assert runfiles.private_dir(root) == root
    finally:
        os.umask(previous)


def test_index_creation_mode_and_lock_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = runfiles.locked
    calls: list[Path] = []

    @contextmanager
    def checked(path: Path) -> Generator[None]:
        calls.append(path)
        assert path.name == "index.lock"
        with locked(path):
            yield

    monkeypatch.setattr(runfiles, "locked", checked)
    previous = os.umask(0)
    try:
        runfiles.append(tmp_path, {"run": "example"})
    finally:
        os.umask(previous)
    assert (tmp_path / "index.jsonl").stat().st_mode & 0o777 == 0o600
    assert runfiles.latest(tmp_path) == {"example": {"run": "example"}}
    assert calls == [tmp_path / "index.lock", tmp_path / "index.lock"]


@pytest.mark.parametrize("failure", ["write", "replace", "directory"])
def test_atomic_failure_preserves_old_file_and_moves_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target = tmp_path / "example.rc"
    target.write_text("old")
    replace = Path.replace
    fsync = os.fsync

    def checked_replace(path: Path, destination: Path) -> Path:
        if failure == "replace" and destination == target:
            raise OSError
        return replace(path, destination)

    def checked_sync(fd: int) -> None:
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (failure == "directory") == directory and failure != "replace":
            raise OSError
        fsync(fd)

    monkeypatch.setattr(Path, "replace", checked_replace)
    monkeypatch.setattr(os, "fsync", checked_sync)
    with pytest.raises(OSError, match=r"^$"):
        runfiles.atomic(target, "new")
    assert target.read_text() == ("new" if failure == "directory" else "old")
    if failure != "directory":
        failed = list(tmp_path.glob(".failed-*/incomplete"))
        assert len(failed) == 1
        assert failed[0].read_text() == "new"
        assert {p.name for p in tmp_path.iterdir()} == {
            target.name,
            failed[0].parent.name,
            "xdg",
        }


def test_two_concurrent_request_claims_have_one_winner(tmp_path: Path) -> None:
    code = """\
import sys
from pathlib import Path
from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.agent.decisions import request
import json
print('ready', flush=True)
sys.stdin.readline()
try:
    runfiles.RunFiles(Path(sys.argv[1]), 'example').receive(
        request.parse(json.loads(sys.argv[2]), host_allows_unsandboxed=False)
    )
except FileExistsError:
    sys.exit(2)
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), json.dumps(BUILD)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            assert child.stdout is not None
            assert child.stdout.readline() == "ready\n"
        for child in children:
            assert child.stdin is not None
            child.stdin.write("go\n")
            child.stdin.flush()
        assert sorted(child.wait(timeout=10) for child in children) == [0, 2]
        assert json.loads((tmp_path / "example.request.json").read_text()) == BUILD
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_claim_mode_ignores_permissive_umask(tmp_path: Path) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    previous = os.umask(0)
    try:
        files.receive(request.parse(BUILD, host_allows_unsandboxed=False))
    finally:
        os.umask(previous)
    assert files.path("request.json").stat().st_mode & 0o777 == 0o600


def test_atomic_opens_only_a_directory_for_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []
    original = os.open

    def checked(path: str | Path, flags: int, mode: int = 0o777) -> int:
        if path == tmp_path:
            assert flags == os.O_RDONLY | os.O_DIRECTORY
            opened.append(flags)
        return original(path, flags, mode)

    monkeypatch.setattr(os, "open", checked)
    runfiles.atomic(tmp_path / "example.rc", "0\n")
    assert opened == [os.O_RDONLY | os.O_DIRECTORY]


@pytest.mark.parametrize(
    "plant", ["regular", "symlink", "fifo", "owner", "oversized", "limit", "missing"]
)
def test_final_answer_copy_is_bounded_owned_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: str
) -> None:
    source = tmp_path / "final"
    destination = tmp_path / "output"
    destination.write_text("old")
    if plant == "symlink":
        source.symlink_to(destination)
    elif plant == "fifo":
        os.mkfifo(source)
    elif plant != "missing":
        source.write_text(
            "x" * (16 * 1024 * 1024 + (1 if plant == "oversized" else 0))
            if plant in ("limit", "oversized")
            else "answer"
        )
    if plant == "owner":
        monkeypatch.setattr(os, "getuid", lambda: source.stat().st_uid + 1)
    if plant in ("regular", "limit"):
        runfiles.copy_final(source, destination)
        assert destination.read_bytes() == source.read_bytes()
        assert destination.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises((OSError, runfiles.Invalid)) as caught:
            runfiles.copy_final(source, destination)
        if plant in ("owner", "fifo", "oversized"):
            assert str(caught.value) == (
                "final answer exceeds 16 MiB"
                if plant == "oversized"
                else "final answer is not an owned regular file"
            )
        assert destination.read_text() == "old"


def test_owned_reader_reports_limits_and_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input"
    source.write_text("a" * 1048577)
    with pytest.raises(runfiles.Invalid) as caught:
        runfiles.read_owned(source, 1048576, "example")
    assert str(caught.value) == "example exceeds 1 MiB"
    source.write_text("regular")
    monkeypatch.setattr(os, "getuid", lambda: source.stat().st_uid + 1)
    with pytest.raises(runfiles.Invalid) as caught:
        runfiles.read_owned(source, 1048576, "example")
    assert str(caught.value) == "example is not an owned regular file"


def test_owned_reader_pins_parent_and_bounds_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input"
    source.write_text("data")
    opened = os.open

    def checked(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
        expected = os.O_RDONLY | os.O_NOFOLLOW
        if path == tmp_path:
            assert dir_fd is None
            expected |= os.O_DIRECTORY
        else:
            assert path == "input"
            assert dir_fd is not None
            expected |= os.O_NONBLOCK
        assert flags == expected
        return opened(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", checked)
    bounded_file_reads(monkeypatch, 1048577)
    assert runfiles.read_owned(source, 1048576, "example") == "data"
