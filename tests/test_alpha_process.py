# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the ps-free process lookup behind the identity hooks."""

import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import identity as ai
from tbhbdev.alpha import process

if TYPE_CHECKING:
    import ctypes
    from collections.abc import Callable


def proc_entry(root: Path, pid: str, stat: str, cmdline: bytes) -> None:
    (root / pid).mkdir()
    (root / pid / "stat").write_text(stat)
    (root / pid / "cmdline").write_bytes(cmdline)


def test_linux_row_reads_parent_and_joined_argv(tmp_path: Path) -> None:
    proc_entry(tmp_path, "7", "7 (py (x) y) S 42 7 7 0 -1\n", b"/bin/zsh\0-c\0eval x\0")
    assert process.linux_row("7", tmp_path) == ("42", "/bin/zsh -c eval x")


def test_linux_row_names_a_kernel_thread_in_brackets(tmp_path: Path) -> None:
    proc_entry(tmp_path, "8", "8 (kworker/0:1) I 2 0 0 0 -1\n", b"")
    assert process.linux_row("8", tmp_path) == ("2", "[kworker/0:1]")


def test_linux_row_replaces_undecodable_bytes(tmp_path: Path) -> None:
    proc_entry(tmp_path, "9", "9 (a) S 1 0\n", b"a\xff\0b")
    assert process.linux_row("9", tmp_path) == ("1", "a� b")


def test_linux_row_none_when_gone_or_malformed(tmp_path: Path) -> None:
    assert process.linux_row("10", tmp_path) is None
    proc_entry(tmp_path, "11", "11 (a) S\n", b"a")
    assert process.linux_row("11", tmp_path) is None
    proc_entry(tmp_path, "12", "garbage", b"a")
    assert process.linux_row("12", tmp_path) is None


def test_linux_row_reads_stat_and_cmdline_by_their_exact_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A case-insensitive file system would accept STAT for stat.
    proc_entry(tmp_path, "15", "15 (a) S 3 0\n", b"a")
    names: list[str] = []
    read_text, read_bytes = Path.read_text, Path.read_bytes

    def spy_text(self: Path, *args: object, **kwargs: object) -> str:
        names.append(self.name)
        return read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def spy_bytes(self: Path) -> bytes:
        names.append(self.name)
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_text", spy_text)
    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    assert process.linux_row("15", tmp_path) == ("3", "a")
    assert names == ["stat", "cmdline"]


def test_linux_row_replaces_undecodable_bytes_in_stat(tmp_path: Path) -> None:
    (tmp_path / "16").mkdir()
    (tmp_path / "16" / "stat").write_bytes(b"16 (\xff) S 4 0\n")
    (tmp_path / "16" / "cmdline").write_bytes(b"")
    assert process.linux_row("16", tmp_path) == ("4", "[\ufffd]")


def test_linux_row_accepts_exactly_state_and_parent(tmp_path: Path) -> None:
    proc_entry(tmp_path, "13", "13 (a) S 5\n", b"a")
    assert process.linux_row("13", tmp_path) == ("5", "a")


def test_linux_row_names_a_thread_by_the_whole_parenthesised_name(
    tmp_path: Path,
) -> None:
    proc_entry(tmp_path, "14", "14 (a(b) I 2 0\n", b"")
    assert process.linux_row("14", tmp_path) == ("2", "[a(b]")


class FakeLibc:
    """libproc and sysctl stand-ins with ctypes' calling conventions."""

    def __init__(
        self,
        ppid: int = 77,
        argv: list[bytes] | None = None,
        path: bytes = b"/usr/bin/git",
    ) -> None:
        self.ppid = ppid
        self.argv = argv
        self.path = path
        self.info_size = process.BSDINFO_SIZE
        self.fail_sysctl_at: int | None = None
        self.sysctl_calls = 0
        self.info_calls: list[tuple[int, int, int]] = []

    def proc_pidinfo(
        self,
        pid: int,
        flavor: int,
        arg: int,
        buf: ctypes.Array[ctypes.c_char],
        size: int,
    ) -> int:
        self.info_calls.append((pid, flavor, arg))
        assert size == process.BSDINFO_SIZE
        struct.pack_into("I", buf, process.PPID_OFFSET, self.ppid)
        return self.info_size

    def proc_pidpath(
        self, pid: int, buf: ctypes.Array[ctypes.c_char], size: int
    ) -> int:
        assert (pid, size) == (321, process.PIDPATH_SIZE)
        buf[: len(self.path)] = self.path
        return len(self.path)

    def sysctl(  # noqa: PLR0913, PLR0917 -- sysctl(3)'s own signature
        self,
        mib: ctypes.Array[ctypes.c_int],
        count: int,
        buf: ctypes.Array[ctypes.c_char] | None,
        size: ctypes._Pointer[ctypes.c_size_t],
        new: None,
        newlen: int,
    ) -> int:
        self.sysctl_calls += 1
        assert (list(mib), count, new, newlen) == ([1, 49, 321], 3, None, 0)
        if self.fail_sysctl_at == self.sysctl_calls or self.argv is None:
            return -1
        raw = struct.pack("i", len(self.argv)) + b"/usr/bin/git\0\0\0"
        raw += b"\0".join(self.argv) + b"\0HOME=/x\0"
        target = size.contents
        if buf is None:
            assert target.value == 0
            target.value = len(raw)
        else:
            assert target.value == len(raw)
            buf[: len(raw)] = raw
        return 0


def fake(libc: FakeLibc) -> ctypes.CDLL:
    return libc  # type: ignore[return-value]


def test_darwin_row_reads_ppid_and_argv() -> None:
    libc = FakeLibc(argv=[b"git", b"commit", b"--amend"])
    assert process.darwin_row("321", fake(libc)) == ("77", "git commit --amend")
    assert libc.info_calls == [(321, process.PROC_PIDTBSDINFO, 0)]


def test_darwin_row_falls_back_to_the_path_when_sysctl_is_denied() -> None:
    for denied_call in (1, 2):
        libc = FakeLibc(argv=[b"git"])
        libc.fail_sysctl_at = denied_call
        assert process.darwin_row("321", fake(libc)) == ("77", "/usr/bin/git")
    assert process.darwin_row("321", fake(FakeLibc(argv=None))) == (
        "77",
        "/usr/bin/git",
    )


def test_darwin_row_keeps_leading_letters_and_a_large_parent() -> None:
    libc = FakeLibc(ppid=3_000_000_000, argv=[b"Xterm", b"a\xff"])
    assert process.darwin_row("321", fake(libc)) == ("3000000000", "Xterm a\ufffd")


def test_darwin_path_reads_exactly_the_returned_bytes() -> None:
    assert process.darwin_path(fake(FakeLibc(path=b"")), 321) is None
    assert process.darwin_path(fake(FakeLibc(path=b"x")), 321) == "x"
    assert process.darwin_path(fake(FakeLibc(path=b"\xff")), 321) == "\ufffd"


def test_darwin_row_falls_back_to_the_path_when_argv_is_empty() -> None:
    assert process.darwin_row("321", fake(FakeLibc(argv=[]))) == ("77", "/usr/bin/git")


def test_darwin_row_none_when_gone_or_nothing_is_readable() -> None:
    gone = FakeLibc(argv=[b"git"])
    gone.info_size = 0
    assert process.darwin_row("321", fake(gone)) is None
    nameless = FakeLibc(argv=None, path=b"")
    assert process.darwin_row("321", fake(nameless)) is None


def test_darwin_row_loads_libc_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = FakeLibc(argv=[b"git"])
    loaded: list[object] = []

    def load(name: object) -> ctypes.CDLL:
        loaded.append(name)
        return fake(libc)

    monkeypatch.setattr(process.ctypes, "CDLL", load)
    assert process.darwin_row("321") == ("77", "git")
    assert loaded == [None]


def tagged(tag: str) -> Callable[[str], tuple[str, str]]:
    def row(pid: str) -> tuple[str, str]:
        return tag, pid

    return row


def test_process_row_picks_the_lookup_by_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "linux_row", tagged("l"))
    monkeypatch.setattr(process, "darwin_row", tagged("d"))
    for platform, expected in (
        ("linux", ("l", "5")),
        ("darwin", ("d", "5")),
        ("win32", None),
    ):
        monkeypatch.setattr(process.sys, "platform", platform)
        assert process.process_row("5") == expected


@pytest.mark.parametrize("pid", ["", "-1", "../self", "1 2", "x"])
def test_process_row_rejects_a_pid_that_is_not_a_number(pid: str) -> None:
    assert process.process_row(pid) is None


@pytest.mark.skipif(
    not os.sys.platform.startswith(("linux", "darwin")), reason="needs /proc or libproc"
)
def test_process_row_on_real_processes() -> None:
    row = process.process_row(str(os.getpid()))
    assert row is not None
    assert row[0] == str(os.getppid())
    assert row[1]
    assert process.process_row("999999999") is None


def fixed(value: tuple[str, str] | None) -> Callable[[str], tuple[str, str] | None]:
    def row(_pid: str) -> tuple[str, str] | None:
        return value

    return row


def test_ps_row_prefers_the_native_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "process_row", fixed(("1", "native")))
    monkeypatch.setattr(ai, "run_ps", fixed(("2", "ps")))
    assert ai.ps_row("5") == ("1", "native")


def test_ps_row_falls_back_to_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def run_ps(pid: str) -> tuple[str, str]:
        asked.append(pid)
        return "2", "ps"

    monkeypatch.setattr(ai, "process_row", fixed(None))
    monkeypatch.setattr(ai, "run_ps", run_ps)
    assert ai.ps_row("5") == ("2", "ps")
    assert asked == ["5"]
