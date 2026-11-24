"""Test temp cleanup in an isolated stand-in for the host filesystem."""

# ruff: noqa: SLF001 -- Exercise the filesystem and kernel guards directly.

from __future__ import annotations

import ctypes
import errno
import os
import stat
import struct
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.agent import temp_cleanup as cleanup

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = 200_000
CUTOFF = NOW - 24 * 60 * 60
OLD = CUTOFF - 1


def age(path: Path, when: float = OLD) -> None:
    os.utime(path, (when, when), follow_symlinks=False)


def old_file(path: Path) -> Path:
    path.touch(mode=0o600)
    age(path)
    return path


@pytest.fixture
def system_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "tmp").mkdir()
    (tmp_path / "proc").mkdir()
    original = os.open

    def open_local(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        return original(tmp_path if path == "/" else path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_local)
    monkeypatch.setattr(cleanup, "PROC", tmp_path / "proc")
    monkeypatch.setattr(cleanup.time, "time", lambda: NOW)
    monkeypatch.setattr(sys, "platform", "linux")
    return temporary


def write_process(root: Path, pid: str, uid: int) -> Path:
    process = root / pid
    (process / "fd").mkdir(parents=True)
    (process / "status").write_text(
        f"Name:\ttest process\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
    )
    return process


def test_sweep_respects_age_and_checks_nested_open_files(
    system_tmp: Path, tmp_path: Path
) -> None:
    older = old_file(system_tmp / "rbz.old")
    boundary = old_file(system_tmp / "rbz.boundary")
    age(boundary, CUTOFF)
    recent = old_file(system_tmp / "rbz.recent")
    age(recent, CUTOFF + 1)
    unrelated = old_file(system_tmp / "tmp-old")
    outside = old_file(tmp_path / "rbz.outside")
    stale = system_tmp / "pytest-of-stale"
    stale.mkdir(mode=0o700)
    old_file(stale / "one")
    link = stale / "pytest-current"
    link.symlink_to(outside)
    age(link)
    age(stale)
    busy = system_tmp / "pytest-of-busy"
    busy.mkdir(mode=0o700)
    held = old_file(busy / "held")
    age(busy)
    fresh_nested = system_tmp / "launchgate-bazel.fresh"
    fresh_nested.mkdir(mode=0o700)
    age(old_file(fresh_nested / "new"), NOW)
    age(fresh_nested)
    cwd = system_tmp / "launchgate-bazel.cwd"
    cwd.mkdir(mode=0o700)
    age(cwd)
    top_link = system_tmp / "launchgate-bazel.link"
    top_link.symlink_to(outside)
    age(top_link)
    shared = old_file(system_tmp / "rbz.shared")
    shared.chmod(0o644)
    fifo = system_tmp / "rbz.fifo"
    os.mkfifo(fifo, mode=0o600)
    age(fifo)

    proc = tmp_path / "proc"
    (proc / "self").symlink_to("123")
    (proc / "122").mkdir()
    other = write_process(proc, "123", os.getuid() + 1)
    (other / "fd" / "4").symlink_to(older)
    process = write_process(proc, "124", os.getuid())
    (process / "cwd").symlink_to(cwd)
    (process / "fd" / "1").symlink_to(held)
    (process / "fd" / "2").symlink_to(system_tmp / "vanished")

    cleanup.sweep()

    assert not stale.exists()
    assert not older.exists()
    assert all(
        path.exists()
        for path in (boundary, recent, unrelated, held, fresh_nested, cwd, shared, fifo)
    )
    assert top_link.is_symlink()
    assert outside.read_bytes() == b""


def test_sweep_uses_darwin_tmp_and_skips_scan_without_candidates(
    system_tmp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = old_file(system_tmp / "rbz.old")
    actual = old_file(tmp_path / "private" / "tmp" / "launchgate-bazel.old")
    calls = []

    def open_files(uid: int) -> set[cleanup.Inode]:
        calls.append(uid)
        return set()

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        patch.setattr(cleanup, "_darwin_open_files", open_files)
        cleanup.sweep()
        cleanup.sweep()
    assert calls == [os.getuid()]
    assert outside.exists()
    assert not actual.exists()


def test_sweep_refuses_symlink_in_place_of_system_tmp(
    system_tmp: Path, tmp_path: Path
) -> None:
    system_tmp.rmdir()
    system_tmp.symlink_to(tmp_path, target_is_directory=True)
    outside = old_file(tmp_path / "rbz.outside")
    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        cleanup.sweep()
    assert outside.exists()


def test_sweep_leaves_candidates_if_open_file_check_fails(
    system_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = old_file(system_tmp / "rbz.old")

    def denied(_uid: int) -> set[cleanup.Inode]:
        raise PermissionError

    monkeypatch.setattr(cleanup, "_linux_open_files", denied)
    with pytest.raises(PermissionError):
        cleanup.sweep()
    assert old.exists()


@contextmanager
def opened(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def test_prune_refuses_outside_path_and_foreign_uid(tmp_path: Path) -> None:
    folder = tmp_path / "tmp"
    folder.mkdir()
    (folder / "rbz.sub").mkdir()
    outside = old_file(tmp_path / "rbz.outside")
    target = old_file(folder / "rbz.foreign")
    device = folder.stat().st_dev
    with opened(folder) as fd:
        foreign = cleanup._Sweep(os.getuid() + 1, CUTOFF, device, set())
        cleanup._prune(fd, target.name, target.stat(), foreign)
        owned = cleanup._Sweep(os.getuid(), CUTOFF, device, set())
        cleanup._prune(fd, "rbz.sub/../../rbz.outside", outside.stat(), owned)
        cleanup._prune(fd, str(outside), outside.stat(), owned)
    assert outside.exists()
    assert target.exists()


def test_changed_candidate_is_left_alone(
    system_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = old_file(system_tmp / "rbz.replaced")
    vanished = old_file(system_tmp / "rbz.vanished")

    def open_files(_uid: int) -> set[cleanup.Inode]:
        target.rename(target.with_name("kept"))
        old_file(target)
        vanished.unlink()
        return set()

    monkeypatch.setattr(cleanup, "_linux_open_files", open_files)
    cleanup.sweep()
    assert target.exists()
    assert not vanished.exists()


@pytest.mark.parametrize("change", ["device", "owner", "kind", "open-dir", "rmdir"])
def test_walk_refuses_unsafe_descendants_and_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    tree = tmp_path / "rbz.tree"
    tree.mkdir(mode=0o700)
    child = tree / "child"
    child.mkdir(mode=0o700)
    age(child)
    age(tree)
    original_stat = os.stat
    changed = [0]

    def stat_child(path: str, **kwargs: object) -> os.stat_result:
        info = original_stat(path, **kwargs)
        if path != child.name:
            return info
        changed[0] += 1
        fields = list(info)
        if change == "device":
            fields[stat.ST_DEV] += 1
        elif change == "owner":
            fields[stat.ST_UID] += 1
        elif change == "kind":
            fields[stat.ST_MODE] = stat.S_IFIFO
        elif change == "open-dir" or changed[0] == 3:
            fields[stat.ST_INO] += 1
        return os.stat_result(fields)

    check = cleanup._Sweep(os.getuid(), CUTOFF, tree.stat().st_dev, set())
    with opened(tmp_path) as fd, monkeypatch.context() as patch:
        patch.setattr(os, "stat", stat_child)
        cleanup._prune(fd, tree.name, original_stat(tree), check)
    assert child.is_dir()


class Libproc:
    class Call:
        def __init__(self, *, data: bytes, probe: int = 0, error: int = 0) -> None:
            self.data = data
            self.probe = probe
            self.error = error
            self.argtypes: tuple[object, ...] = ()

        def __call__(self, *args: int) -> int:
            *_, buffer, size = args
            if buffer is None:
                return self.probe
            if self.error:
                ctypes.set_errno(self.error)
                return 0
            used = min(size, len(self.data))
            ctypes.memmove(buffer, self.data, used)
            return used

    def __init__(self, pid: int) -> None:
        self.proc_listpids = self.Call(data=struct.pack("=i", pid), probe=4)
        self.fd = self.Call(data=struct.pack("=iIiI", 0, 2, 1, 1), probe=24)
        payload = struct.pack("=I4xQ", 22, 33)
        self.cwd = self.Call(data=payload)
        self.proc_pidfdinfo = self.Call(data=b"\0" * 24 + struct.pack("=I4xQ", 44, 55))

        def proc_pidinfo(pid: int, flavor: int, arg: int, *buffer: int) -> int:
            call = self.cwd if flavor == 9 else self.fd
            return call(pid, flavor, arg, *buffer)

        self.proc_pidinfo = proc_pidinfo


@pytest.mark.parametrize("race", [False, True])
def test_darwin_reads_vnode_ids_from_libproc(
    monkeypatch: pytest.MonkeyPatch, race: bool
) -> None:
    probe = Libproc(pid=123)
    if race:
        probe.cwd.error = errno.ESRCH
        probe.proc_pidfdinfo.error = errno.EBADF
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: probe)
    assert cleanup._darwin_open_files(os.getuid()) == (
        set() if race else {(22, 33), (44, 55)}
    )


def test_libproc_returns_no_data_for_ended_process() -> None:
    assert cleanup._proc_info(Libproc.Call(data=b"")) == b""


def test_libproc_fails_closed_when_access_is_denied() -> None:
    with pytest.raises(PermissionError):
        cleanup._proc_info(Libproc.Call(data=b"", error=errno.EPERM), size=32)
