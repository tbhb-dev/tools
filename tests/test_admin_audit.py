"""Durability, ownership, and append-only descriptor checks."""

# cspell:words fspath

import hashlib
import json
import os
import stat
from datetime import datetime
lazy from pathlib import Path

import httpx
import pytest

from tbhbdev.admin import policy, request
from tbhbdev.admin.audit import Audit, RefusedError, record
from tbhbdev.alpha import gh_app


def test_log_flags_modes_and_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "state/tbhbdev/admin"
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
    entry = record("PATCH", decision, "maintenance", body)
    audit.append(entry)
    audit.append({**entry, "phase": "after", "status": 200})
    assert (
        calls
        == [
            (str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, 0o777),
            (
                "calls.jsonl",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            ),
        ]
        * 2
    )
    assert len(syncs) == 4
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    logfile = directory / "calls.jsonl"
    assert stat.S_IMODE(logfile.stat().st_mode) == 0o600
    lines = logfile.read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
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
    runtime = request.Runtime(Audit(tmp_path / "admin"), env)

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
