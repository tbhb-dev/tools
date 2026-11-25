"""The registered worker is found by its ancestors' executables."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import worker_identity
from tbhbdev.alpha.worker_identity import Process

if TYPE_CHECKING:
    from collections.abc import Callable

WORKER = "/opt/worker/bin/worker"


def tree(*chain: tuple[int, str]) -> worker_identity.Reader:
    """A reader for processes CHAIN, each child first; the last has parent 0."""
    table: dict[int, Process] = {}
    for index, (pid, exe) in enumerate(chain):
        parent = chain[index + 1][0] if index + 1 < len(chain) else 0
        table[pid] = Process(parent, exe)
    return table.get


def why(registered: str | None, read: worker_identity.Reader, pid: int = 100) -> str:
    result = worker_identity.why_not(registered, pid, read)
    assert result is not None
    return result


def test_a_parent_running_the_worker_matches() -> None:
    read = tree((100, "/usr/bin/tbhbdev"), (50, WORKER), (1, "/sbin/init"))
    assert worker_identity.why_not(WORKER, 100, read) is None


def test_the_worker_three_levels_up_matches() -> None:
    read = tree(
        (100, "/usr/bin/tbhbdev"),
        (90, "/bin/sh"),
        (80, "/usr/bin/env"),
        (70, WORKER),
        (1, "/sbin/init"),
    )
    assert worker_identity.why_not(WORKER, 100, read) is None


def test_the_walk_checks_pid_one() -> None:
    read = tree((100, "/usr/bin/tbhbdev"), (1, WORKER))
    assert worker_identity.why_not(WORKER, 100, read) is None


def test_the_process_itself_does_not_count() -> None:
    read = tree((100, WORKER), (50, "/bin/sh"), (1, "/sbin/init"))
    assert "no ancestor process runs" in why(WORKER, read)


def test_other_ancestors_do_not_match() -> None:
    read = tree((100, "/usr/bin/tbhbdev"), (50, "/bin/bash"), (1, "/sbin/init"))
    assert why(WORKER, read) == (
        f"no ancestor process runs the registered worker {WORKER}"
    )


def test_a_prefix_or_a_suffix_of_the_path_does_not_match() -> None:
    read = tree(
        (100, "/usr/bin/tbhbdev"),
        (60, WORKER + "2"),
        (50, "/x" + WORKER),
        (1, "/sbin/init"),
    )
    assert "no ancestor" in why(WORKER, read)


def test_an_unregistered_worker_never_matches() -> None:
    read = tree((100, "/usr/bin/tbhbdev"), (50, WORKER), (1, "/sbin/init"))
    assert why(None, read) == "no [agent.worker] path is registered in the config"


def test_the_exe_and_the_registered_path_compare_by_realpath(tmp_path: Path) -> None:
    real = tmp_path / "real-worker"
    real.write_text("")
    link = tmp_path / "link-worker"
    link.symlink_to(real)
    other = tmp_path / "other"
    other.write_text("")
    read = tree((100, "/usr/bin/tbhbdev"), (50, str(link)), (1, "/sbin/init"))
    assert worker_identity.why_not(str(real), 100, read) is None
    read = tree((100, "/usr/bin/tbhbdev"), (50, str(real)), (1, "/sbin/init"))
    assert worker_identity.why_not(str(link), 100, read) is None
    assert "no ancestor" in why(str(other), read)


def test_a_loop_in_the_parents_is_no_match() -> None:
    table = {
        100: Process(50, "/usr/bin/tbhbdev"),
        50: Process(60, "/bin/sh"),
        60: Process(50, "/bin/sh"),
    }
    assert "no ancestor" in why(WORKER, table.get)


def test_a_process_that_is_its_own_parent_is_no_match() -> None:
    assert "no ancestor" in why(WORKER, {100: Process(100, "/x")}.get)


def test_a_walk_that_never_reaches_pid_one_ends_at_a_missing_parent() -> None:
    table = {100: Process(50, "/x"), 50: Process(0, "/y")}
    assert "no ancestor" in why(WORKER, table.get)


def test_an_unreadable_process_is_no_match() -> None:
    assert "no ancestor" in why(WORKER, {}.get)


def test_an_unreadable_parent_is_no_match_even_with_the_worker_above() -> None:
    table = {100: Process(50, "/x"), 1: Process(0, WORKER)}
    assert "no ancestor" in why(WORKER, table.get)


def test_an_unreadable_grandparent_is_no_match() -> None:
    table = {100: Process(50, "/x"), 50: Process(40, "/y")}
    assert "no ancestor" in why(WORKER, table.get)


def test_a_parent_with_an_unknown_exe_stops_the_walk_with_a_reason() -> None:
    read = tree((100, "/x"), (50, ""), (40, WORKER), (1, "/sbin/init"))
    assert "process 50 could not be read" in why(WORKER, read)
    assert "no ancestor" in why("", read)


def test_an_unreadable_process_is_named_in_the_reason() -> None:
    assert "process 100 could not be read" in why(WORKER, {}.get)


@pytest.mark.parametrize("text", ["a", "A.b_c:d-e", "x" * 128, "run:42"])
def test_a_workflow_id_is_one_to_128_safe_characters(text: str) -> None:
    assert worker_identity.workflow(text) == text


@pytest.mark.parametrize("text", ["", "x" * 129, "a b", "a/b", "a\n", "é", "a;b"])
def test_any_other_workflow_id_is_refused(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        worker_identity.workflow(text)
    assert str(caught.value) == "must be 1 to 128 characters of A-Za-z0-9._:-"


def test_the_launcher_and_closer_name_is_worker_slash_id() -> None:
    assert worker_identity.name("run:7") == "worker/run:7"


def write_proc(
    root: Path, pid: int, stat: str | None, exe: str | None = "/bin/x"
) -> None:
    folder = root / str(pid)
    folder.mkdir()
    if stat is not None:
        (folder / "stat").write_text(stat)
    if exe is not None:
        (folder / "exe").symlink_to(exe)


def test_the_linux_reader_takes_the_parent_and_the_exe(tmp_path: Path) -> None:
    write_proc(tmp_path, 7, "7 (a) b) S 42 7 7 0 -1\n", exe="/opt/w (deleted)")
    assert worker_identity.linux_reader(str(tmp_path))(7) == Process(
        42, "/opt/w (deleted)"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_the_linux_reader_defaults_to_proc() -> None:
    process = worker_identity.linux_reader()(os.getpid())
    assert process is not None
    assert process.ppid == os.getppid()
    assert process.exe == Path(f"/proc/{os.getpid()}/exe").readlink().as_posix()


@pytest.mark.parametrize(
    ("stat", "exe"),
    [
        (None, "/bin/x"),
        ("7 (a) S\n", "/bin/x"),
        ("7 (a) S x 1\n", "/bin/x"),
        ("7 a S 1\n", "/bin/x"),
        ("7 (a) S 1\n", None),
    ],
)
def test_the_linux_reader_fails_closed(
    tmp_path: Path, stat: str | None, exe: str | None
) -> None:
    write_proc(tmp_path, 7, stat, exe)
    assert worker_identity.linux_reader(str(tmp_path))(7) is None


def test_the_linux_reader_has_no_missing_process(tmp_path: Path) -> None:
    assert worker_identity.linux_reader(str(tmp_path))(7) is None


def fake_darwin(
    answers: dict[tuple[str, int], str | None], paths: dict[int, str | None]
) -> worker_identity.Reader:
    def ps(field: str, pid: int) -> str | None:
        return answers.get((field, pid))

    return worker_identity.darwin_reader(ps, paths.get)


def test_the_darwin_reader_takes_the_parent_and_the_pidpath() -> None:
    read = fake_darwin({("ppid", 7): "42"}, {7: "/opt/w"})
    assert read(7) == Process(42, "/opt/w")


@pytest.mark.parametrize("path", [None, ""])
def test_the_darwin_reader_fails_closed_on_an_unreadable_pidpath(
    path: str | None,
) -> None:
    assert fake_darwin({("ppid", 7): "42"}, {7: path})(7) is None


def test_a_failed_darwin_lookup_in_the_middle_refuses_despite_the_worker_above() -> (
    None
):
    paths = {100: "/x", 50: None, 40: WORKER, 1: "/sbin/init"}
    parents = {100: "50", 50: "40", 40: "1", 1: "0"}
    read = worker_identity.darwin_reader(lambda _f, pid: parents.get(pid), paths.get)
    assert "process 50 could not be read" in why(WORKER, read)


def test_a_failed_linux_lookup_in_the_middle_refuses_despite_the_worker_above(
    tmp_path: Path,
) -> None:
    write_proc(tmp_path, 100, "100 (a) S 50\n", exe="/x")
    write_proc(tmp_path, 50, "50 (a) S 40\n", exe=None)
    write_proc(tmp_path, 40, "40 (a) S 1\n", exe=WORKER)
    write_proc(tmp_path, 1, "1 (a) S 0\n", exe="/sbin/init")
    read = worker_identity.linux_reader(str(tmp_path))
    assert "process 50 could not be read" in why(WORKER, read)


@pytest.mark.parametrize("parent", [None, "", "x1", "-1"])
def test_the_darwin_reader_fails_closed(parent: str | None) -> None:
    assert fake_darwin({("ppid", 7): parent}, {7: "/opt/w"})(7) is None


def cdll_of(libc: object) -> Callable[..., object]:

    def cdll(*_args: object, **_kwargs: object) -> object:
        return libc

    return cdll


def test_pidpath_decodes_the_libproc_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    class Libc:
        def proc_pidpath(
            self,
            pid: int,
            buffer: worker_identity.ctypes.Array[worker_identity.ctypes.c_char],
            size: int,
        ) -> int:
            assert (pid, size) == (7, 4096)
            buffer.value = b"/opt/w"
            return 6

    monkeypatch.setattr(worker_identity.ctypes, "CDLL", cdll_of(Libc()))
    assert worker_identity._pidpath(7) == "/opt/w"  # noqa: SLF001


def test_pidpath_is_unknown_when_libproc_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Libc:
        def proc_pidpath(self, *_: object) -> int:
            return 0

    monkeypatch.setattr(worker_identity.ctypes, "CDLL", cdll_of(Libc()))
    assert worker_identity._pidpath(7) is None  # noqa: SLF001


def test_pidpath_is_unknown_without_libproc(monkeypatch: pytest.MonkeyPatch) -> None:
    def cdll(*_: object, **__: object) -> None:
        raise OSError

    monkeypatch.setattr(worker_identity.ctypes, "CDLL", cdll)
    assert worker_identity._pidpath(7) is None  # noqa: SLF001


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS only")
@pytest.mark.parametrize("launch", ["path", "relative"])
def test_live_a_worker_launched_by_path_or_relative_name_resolves_absolute(
    tmp_path: Path, launch: str
) -> None:
    exe = tmp_path / "review-worker"
    shutil.copy("/bin/sleep", exe)
    # A copied system binary has an invalid signature and is killed on launch.
    # cspell:ignore codesign
    subprocess.run(["/usr/bin/codesign", "--force", "-s", "-", str(exe)], check=True)
    argv0 = "review-worker" if launch == "path" else "./review-worker"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    child = subprocess.Popen([argv0, "30"], cwd=tmp_path, env=env)
    try:
        process = worker_identity.darwin_reader()(child.pid)
        assert process is not None
        assert os.path.realpath(process.exe) == os.path.realpath(exe)
    finally:
        child.kill()
        child.wait()


def test_ps_runs_the_absolute_ps_with_the_field_and_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, " 42\n", "")

    monkeypatch.setattr(worker_identity.subprocess, "run", run)
    assert worker_identity._ps("ppid", 7) == "42"  # noqa: SLF001
    assert calls == [
        (
            ["/bin/ps", "-o", "ppid=", "-p", "7"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": True,
            },
        )
    ]


@pytest.mark.parametrize(
    "error",
    [
        OSError("no ps"),
        subprocess.TimeoutExpired("ps", 5),
        subprocess.CalledProcessError(1, "darwin"),
    ],
)
def test_ps_failure_is_no_answer(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def run(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(worker_identity.subprocess, "run", run)
    assert worker_identity._ps("comm", 7) is None  # noqa: SLF001


def constant(process: Process) -> worker_identity.Reader:
    def read(_pid: int) -> Process:
        return process

    return read


@pytest.mark.parametrize(
    ("platform", "kind"), [("linux", "linux"), ("darwin", "darwin")]
)
def test_the_reader_follows_the_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, kind: str
) -> None:
    monkeypatch.setattr(worker_identity.sys, "platform", platform)
    monkeypatch.setattr(
        worker_identity, "linux_reader", lambda: constant(Process(1, "l"))
    )
    monkeypatch.setattr(
        worker_identity, "darwin_reader", lambda: constant(Process(2, "p"))
    )
    process = worker_identity.reader()(1)
    assert process == (Process(1, "l") if kind == "linux" else Process(2, "p"))


def test_the_refusal_reads_this_process_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    read = tree((os.getpid(), "/x"), (50, WORKER), (1, "/sbin/init"))
    monkeypatch.setattr(worker_identity, "reader", lambda: read)
    assert worker_identity.refusal(WORKER) is None
    assert worker_identity.refusal("/other") is not None
