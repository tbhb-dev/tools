"""Durability, ownership, and append-only descriptor checks."""

# cspell:words fspath

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import threading
from datetime import datetime
lazy from pathlib import Path

import httpx
import pytest

from tbhbdev.admin import policy, request
from tbhbdev.admin.audit import GENESIS, Audit, RefusedError, record
from tbhbdev.alpha import gh_app


def unchained(lines: list[str]) -> list[dict[str, object]]:
    """Parsed records without their ``prev`` link."""
    return [
        {key: value for key, value in json.loads(line).items() if key != "prev"}
        for line in lines
    ]


def test_log_flags_modes_and_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "state/tbhbdev/admin"
    directory.mkdir(mode=0o700, parents=True)
    audit = Audit(directory)
    original_open = os.open
    calls: list[tuple[str, int, int]] = []
    syncs: list[int] = []

    def opened(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        calls.append((os.fspath(path), flags, mode))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fsync", syncs.append)
    body = b'{"description":"private request text"}'
    decision = policy.check(
        "PATCH", "/repos/tbhb-dev/widget", {"description": "example"}
    )
    entry = record("PATCH", decision, "maintenance", body, "/repos/tbhb-dev/widget")
    audit.append(entry)
    audit.append({**entry, "phase": "after", "status": 200})
    own = [call for call in calls if call[0] in {str(directory), "calls.jsonl"}]
    # Each call syncs the directory chain twice over: once while checking
    # ancestors and once for the opened directory, so only the log open matters.
    assert (
        "calls.jsonl",
        os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    ) in own
    opens = [call for call in own if call[0] == str(directory)]
    assert opens
    assert {call[1:] for call in opens} == {
        (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, 0o777)
    }
    chain = len((directory, *directory.parents))
    assert len(syncs) == 2 * (chain + 2)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    logfile = directory / "calls.jsonl"
    assert stat.S_IMODE(logfile.stat().st_mode) == 0o600
    lines = logfile.read_text().splitlines()
    assert unchained(lines) == [
        entry,
        {**entry, "phase": "after", "status": 200},
    ]
    assert entry == {
        "id": entry["id"],
        "time": entry["time"],
        "uid": os.getuid(),
        "reason": "maintenance",
        "method": "PATCH",
        "path": "/repos/tbhb-dev/widget",
        "decision": "allow",
        "code": "allowed",
        "status": None,
        "phase": "before",
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
    assert len(str(entry["id"])) == 32
    offset = datetime.fromisoformat(str(entry["time"])).utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert "private request text" not in logfile.read_text()


def test_short_writes_finish_one_whole_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.write

    def short(fd: int, data: bytes) -> int:
        return original(fd, data[:5])

    monkeypatch.setattr(os, "write", short)
    Audit(tmp_path / "admin").append({"first": "record with a long value"})
    monkeypatch.undo()
    lines = (tmp_path / "admin/calls.jsonl").read_text().splitlines()
    assert unchained(lines) == [{"first": "record with a long value"}]


def test_failed_write_cuts_off_the_partial_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = Audit(tmp_path / "admin")
    audit.append({"n": 1})
    original = os.write
    calls: list[int] = []

    def flaky(fd: int, data: bytes) -> int:
        calls.append(1)
        if len(calls) == 2:
            raise OSError
        return original(fd, data[:4])

    monkeypatch.setattr(os, "write", flaky)
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        audit.append({"n": 2})
    monkeypatch.undo()
    audit.append({"n": 3})
    lines = (tmp_path / "admin/calls.jsonl").read_text().splitlines()
    assert unchained(lines) == [{"n": 1}, {"n": 3}]
    assert json.loads(lines[1])["prev"] == hashlib.sha256(lines[0].encode()).hexdigest()


def test_a_log_swapped_for_a_symlink_to_the_held_file_is_refused(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "admin"
    directory.mkdir()
    log = directory / "calls.jsonl"
    log.write_text("")
    directory_fd = os.open(directory, os.O_RDONLY)
    descriptor = os.open(log, os.O_RDWR)
    try:
        log.rename(directory / "old.jsonl")
        log.symlink_to("old.jsonl")
        with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
            Audit.same_file(directory_fd, descriptor)
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def test_same_file_resolves_the_log_inside_the_directory_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "admin"
    directory.mkdir()
    (directory / "calls.jsonl").write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "calls.jsonl").write_text("")
    monkeypatch.chdir(elsewhere)
    directory_fd = os.open(directory, os.O_RDONLY)
    descriptor = os.open(directory / "calls.jsonl", os.O_RDWR)
    try:
        Audit.same_file(directory_fd, descriptor)
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def test_new_directories_are_private_whatever_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        directory = tmp_path / "a/admin"
        Audit(directory).append({"n": 1})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_created_directories_and_their_parents_are_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(Audit, "sync_directory", staticmethod(synced.append))
    directory = tmp_path / "a/b/c"
    audit = Audit(directory)
    audit.append({"n": 1})
    assert set(synced) == {directory, *directory.parents}
    synced.clear()
    audit.append({"n": 2})
    assert set(synced) == {directory, *directory.parents}


def test_failed_ancestor_sync_is_retried_on_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "a/b"
    audit = Audit(directory)
    real = Audit.sync_directory

    def failing(path: Path) -> None:
        if path == directory.parent:
            raise OSError
        real(path)

    monkeypatch.setattr(Audit, "sync_directory", staticmethod(failing))
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        audit.append({"n": 1})
    synced: list[Path] = []
    monkeypatch.setattr(Audit, "sync_directory", staticmethod(synced.append))
    audit.append({"n": 2})
    assert directory.parent in synced


def skip(path: Path) -> None:
    """Stand in for a directory sync."""


def test_log_file_is_synced_after_each_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    original = os.fsync

    def recording(fd: int) -> None:
        synced.append(fd)
        original(fd)

    monkeypatch.setattr(os, "fsync", recording)
    monkeypatch.setattr(Audit, "sync_directory", staticmethod(skip))
    Audit(tmp_path / "admin").append({"n": 1})
    # One sync for the log file and one for its directory.
    assert len(synced) == 2


def test_overlapping_writer_waits_and_the_rollback_is_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "admin"
    Audit(directory).append({"n": 1})
    logfile = directory / "calls.jsonl"
    log_inode = logfile.stat().st_ino
    entered = threading.Event()
    release = threading.Event()
    log_syncs: list[str] = []
    original = os.fsync

    def sync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == log_inode:
            name = threading.current_thread().name
            log_syncs.append(name)
            if name == "writer-a" and log_syncs.count("writer-a") == 1:
                entered.set()
                assert release.wait(timeout=10)
                raise OSError
        original(fd)

    def write_a() -> None:
        with contextlib.suppress(RefusedError):
            Audit(directory).append({"n": 2})

    monkeypatch.setattr(os, "fsync", sync)
    writer_a = threading.Thread(target=write_a, name="writer-a")
    writer_b = threading.Thread(
        target=lambda: Audit(directory).append({"n": 3}), name="writer-b"
    )
    writer_a.start()
    assert entered.wait(timeout=10)
    writer_b.start()
    writer_b.join(timeout=0.5)
    assert writer_b.is_alive()
    release.set()
    writer_a.join(timeout=10)
    writer_b.join(timeout=10)
    monkeypatch.undo()
    lines = logfile.read_text().splitlines()
    assert unchained(lines) == [{"n": 1}, {"n": 3}]
    # A syncs once for the failed write and once after the rollback.
    assert log_syncs.count("writer-a") == 2


def test_a_log_replaced_before_the_lock_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "admin"
    audit = Audit(directory)
    audit.append({"n": 1})
    original = fcntl.flock

    def swapping(fd: int, operation: int) -> None:
        original(fd, operation)
        (directory / "calls.jsonl").rename(directory / "old.jsonl")
        replacement = directory / "calls.jsonl"
        replacement.write_text("")
        replacement.chmod(0o600)

    monkeypatch.setattr(fcntl, "flock", swapping)
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        audit.append({"n": 2})
    monkeypatch.undo()
    assert (directory / "calls.jsonl").read_text() == ""


def test_writers_hold_an_exclusive_lock_over_the_whole_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[int] = []
    original = fcntl.flock

    def recording(fd: int, operation: int) -> None:
        modes.append(operation)
        original(fd, operation)

    monkeypatch.setattr(fcntl, "flock", recording)
    Audit(tmp_path / "admin").append({"n": 1})
    assert modes == [fcntl.LOCK_EX]


def test_unfinished_tail_is_ended_before_the_next_record(tmp_path: Path) -> None:
    directory = tmp_path / "admin"
    directory.mkdir(mode=0o700)
    logfile = directory / "calls.jsonl"
    logfile.write_text('{"n": 1}\n{"par')
    logfile.chmod(0o600)
    Audit(directory).append({"n": 2})
    lines = logfile.read_text().splitlines()
    assert lines[0] == '{"n": 1}'
    assert lines[1] == '{"par'
    assert unchained(lines[2:]) == [{"n": 2}]
    assert json.loads(lines[2])["prev"] == hashlib.sha256(b'{"par').hexdigest()


def test_records_form_a_hash_chain(tmp_path: Path) -> None:
    audit = Audit(tmp_path / "admin")
    for number in range(3):
        audit.append({"n": number})
    lines = (tmp_path / "admin/calls.jsonl").read_text().splitlines()
    links = [json.loads(line)["prev"] for line in lines]
    assert links[0] == "0" * 64
    digests = [hashlib.sha256(line.encode()).hexdigest() for line in lines[:2]]
    assert links[1:] == digests


def test_sync_directory_opens_without_following_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flags: list[int] = []
    original = os.open

    def opened(path: Path, mode: int) -> int:
        flags.append(mode)
        return original(path, mode)

    monkeypatch.setattr(os, "open", opened)
    Audit.sync_directory(tmp_path)
    assert flags == [os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW]


@pytest.mark.parametrize(
    "kind",
    [
        "directory_mode",
        "file_mode",
        "owner",
        "symlink",
        "directory_symlink",
        "hard_link",
        "not_regular",
    ],
)
def test_unsafe_log_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory = tmp_path / "admin"
    directory.mkdir(mode=0o700)
    logfile = directory / "calls.jsonl"
    logfile.touch(mode=0o600)
    match kind:
        case "directory_mode":
            directory.chmod(0o755)
        case "file_mode":
            logfile.chmod(0o644)
        case "owner":
            monkeypatch.setattr(os, "getuid", lambda: directory.stat().st_uid + 1)
        case "symlink":
            logfile.rename(directory / "saved")
            logfile.symlink_to(directory / "saved")
        case "directory_symlink":
            directory.rename(tmp_path / "saved")
            directory.symlink_to(tmp_path / "saved", target_is_directory=True)
        case "hard_link":
            os.link(logfile, directory / "linked")
        case "not_regular":
            logfile.rename(directory / "saved")
            logfile.mkdir()
        case _:
            pytest.fail("unknown fixture case")
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        Audit(directory).append({"example": True})


@pytest.mark.parametrize("failure", ["open", "write", "sync", "short_write"])
def test_fail_closed_before_any_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> int:
        raise OSError

    def no_store() -> gh_app.Reader:
        pytest.fail("refusal reached the secret reader")

    def no_network(_req: httpx.Request) -> httpx.Response:
        pytest.fail("refusal reached HTTP")

    env = gh_app.Env(
        tmp_path,
        lambda: 0,
        no_store,
        gh_app.run_openssl,
        httpx.MockTransport(no_network),
    )
    runtime = request.Runtime(Audit(tmp_path / "admin"), env, tmp_path / "enabled")

    def short_write(_descriptor: int, _data: bytes) -> int:
        return 0

    if failure == "short_write":
        monkeypatch.setattr(os, "write", short_write)
    else:
        monkeypatch.setattr(
            os,
            {"open": "open", "write": "write", "sync": "fsync"}[failure],
            unavailable,
        )
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        request.call(runtime, "GET", "/repos/tbhb-dev/widget", None, "test")


def test_validate_file_owner_and_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file"
    path.touch(mode=0o600)
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        Audit.validate(path.stat(), mode=0o600)
    monkeypatch.undo()
    path.chmod(0o700)
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        Audit.validate(path.stat(), mode=0o700)


def test_a_log_rotated_during_the_write_refuses_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "admin"
    audit = Audit(directory)
    audit.append({"n": 0})
    original = Audit.write_all

    def rotate(descriptor: int, data: bytes) -> None:
        original(descriptor, data)
        (directory / "calls.jsonl").rename(directory / "old.jsonl")
        (directory / "calls.jsonl").write_text("")

    monkeypatch.setattr(Audit, "write_all", staticmethod(rotate))
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        audit.append({"n": 1})
    # The record stays in the rotated file; it is not cut off by a rollback.
    assert (directory / "old.jsonl").read_text().count("\n") == 2
    assert (directory / "calls.jsonl").read_text() == ""


def test_record_keeps_the_path_it_is_given() -> None:
    decision = policy.check("GET", "/repos/tbhb-dev/widget/contents/README.md")
    entry = record(
        "GET", decision, "test", b"", "/repos/tbhb-dev/widget/contents/README.md"
    )
    assert entry["path"] == "/repos/tbhb-dev/widget/contents/README.md"


@pytest.mark.parametrize("last", [b'{"n": 1} ', b'{"n": "X"}X', b"\t"])
def test_chain_covers_the_whole_last_line(tmp_path: Path, last: bytes) -> None:
    logfile = tmp_path / "calls.jsonl"
    logfile.write_bytes(b'{"n": 0}\n' + last + b"\n")
    descriptor = os.open(logfile, os.O_RDONLY)
    try:
        assert Audit.chain_head(descriptor) == hashlib.sha256(last).hexdigest()
    finally:
        os.close(descriptor)


def head_of(content: bytes, tmp_path: Path) -> str:
    logfile = tmp_path / "head.jsonl"
    logfile.write_bytes(content)
    descriptor = os.open(logfile, os.O_RDONLY)
    try:
        return Audit.chain_head(descriptor)
    finally:
        os.close(descriptor)


def test_an_empty_log_starts_the_chain_and_an_empty_line_does_not(
    tmp_path: Path,
) -> None:
    assert head_of(b"", tmp_path) == GENESIS
    assert head_of(b"\n", tmp_path) == hashlib.sha256(b"").hexdigest()
    assert head_of(b'{"n": 0}\n\n', tmp_path) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("length", [1, 4095, 4096, 4097, 12288, 12289])
def test_the_head_reads_a_last_line_of_any_length(tmp_path: Path, length: int) -> None:
    last = b"x" * length
    content = b'{"n": 0}\n' + b'{"n": 1}\n' * 3 + last + b"\n"
    assert head_of(content, tmp_path) == hashlib.sha256(last).hexdigest()
    assert head_of(last + b"\n", tmp_path) == hashlib.sha256(last).hexdigest()


def test_a_short_read_of_the_tail_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def empty_read(_fd: int, _count: int, _offset: int) -> bytes:
        return b""

    monkeypatch.setattr(os, "pread", empty_read)
    with pytest.raises(RefusedError, match=r"^audit_unavailable$"):
        head_of(b"abc\n", tmp_path)


def test_a_caller_supplied_prev_is_replaced(tmp_path: Path) -> None:
    audit = Audit(tmp_path / "admin")
    audit.append({"n": 1, "prev": "forged"})
    entry = json.loads((tmp_path / "admin/calls.jsonl").read_text())
    assert entry["prev"] == GENESIS
