"""Output-base pruning uses disposable directories and fake build tools."""

import ctypes
import json
import os
import subprocess
import sys
import time
from unittest.mock import Mock
lazy from collections.abc import Iterator
lazy from pathlib import Path

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import bazel_prune as prune


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    commands = tmp_path / "commands"
    commands.mkdir()
    chmod = prune.shutil.which("chmod")
    assert chmod is not None
    (commands / "chmod").symlink_to(chmod)
    monkeypatch.setenv("PATH", str(commands))


@pytest.fixture
def base(tmp_path: Path) -> Path:
    directory = tmp_path / "outputs" / ("a" * 32)
    directory.mkdir(parents=True)
    (directory / "DO_NOT_BUILD_HERE").write_text(str(tmp_path / "workspace"))
    (directory / "server").mkdir()
    (directory / "data").write_bytes(b"cache")
    return directory


def invoke(base: Path, *flags: str) -> int:
    return prune.main(["--output-root", str(base.parent), *flags], "prune")


def age(base: Path, days: int = 15) -> None:
    timestamp = time.time() - days * 86400
    for path in base.iterdir():
        os.utime(path, (timestamp, timestamp))
    os.utime(base, (timestamp, timestamp))


@pytest.fixture
def child(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with subprocess.Popen(
        [sys.executable, "-c", "print('ready', flush=True); input()"],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        yield process
        if process.poll() is None:
            process.communicate("\n", timeout=10)


@pytest.mark.parametrize("existing", [False, True])
def test_prunes_gone_and_idle(
    base: Path,
    existing: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if existing:
        (base.parents[1] / "workspace").mkdir()
        age(base)
    assert invoke(base) == 0
    assert not base.exists()
    output = capsys.readouterr().out
    assert f"pruned {'idle' if existing else 'gone'} " in output
    assert "freed " in output


def test_keeps_fresh(base: Path) -> None:
    (base.parents[1] / "workspace").mkdir()
    assert invoke(base) == 0
    assert base.exists()


def test_keeps_live_workspace(
    base: Path,
    child: subprocess.Popen[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert child.poll() is None
    age(base)
    assert invoke(base) == 0
    assert base.exists()
    assert "keep busy " in capsys.readouterr().out


@pytest.mark.parametrize("stops", [False, True])
def test_server_shutdown(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    stops: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path = base.parents[1]
    workspace = tmp_path / "gone"
    (base / "DO_NOT_BUILD_HERE").write_text(str(workspace))
    stop = tmp_path / "stop"
    binary = tmp_path / "bin"
    binary.mkdir()
    record = tmp_path / "argv.json"
    tool = binary / "bazel"
    tool.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        + (f"pathlib.Path({str(stop)!r}).touch()\n" if stops else "")
    )
    tool.chmod(0o755)
    chmod = prune.shutil.which("chmod")
    assert chmod is not None
    (binary / "chmod").symlink_to(chmod)
    monkeypatch.setenv("PATH", str(binary))
    script = (
        "import pathlib, sys, time\n"
        "while not pathlib.Path(sys.argv[1]).exists():\n    time.sleep(0.01)\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(stop)],
        cwd=tmp_path,
    ) as server:
        (base / "server/server.pid.txt").write_text(str(server.pid))
        original = prune.shutdown

        def shutdown(directory: Path, workspace: Path) -> bool:
            result = original(directory, workspace)
            if stops:
                server.wait(timeout=10)
            return result

        monkeypatch.setattr(prune, "shutdown", shutdown)
        try:
            assert invoke(base) == 0
            assert record.read_text() == json.dumps(
                [f"--output_base={base}", "shutdown"]
            )
            assert base.exists() is not stops
            output = capsys.readouterr().out
            assert ("pruned gone " if stops else "keep server-alive ") in output
        finally:
            stop.touch()
            server.wait(timeout=10)


def test_dry_run(base: Path, tmp_path: Path) -> None:
    before = {path: path.lstat() for path in base.rglob("*")}
    assert invoke(base, "--dry-run") == 0
    assert {path: path.lstat() for path in base.rglob("*")} == before
    assert not (tmp_path / "home").exists()


def test_symlinks_readonly_and_other_entries(
    base: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "DO_NOT_BUILD_HERE").write_text(str(tmp_path / "gone"))
    target = outside / "file"
    target.write_text("keep")
    target.chmod(0o444)
    (base / "link").symlink_to(outside, target_is_directory=True)
    (base / "data").chmod(0o444)
    for name in ("install", "disk-cache", "cache/repos", "b" * 32):
        (base.parent / name).mkdir(parents=True)
    (base.parent / ("c" * 32)).symlink_to(outside, target_is_directory=True)
    assert invoke(base) == 0
    assert not base.exists()
    assert target.read_text() == "keep"
    assert target.stat().st_mode & 0o777 == 0o444
    assert (base.parent / "install").is_dir()
    assert (base.parent / "cache/repos").is_dir()
    assert (base.parent / "disk-cache").is_dir()
    assert (base.parent / ("b" * 32)).is_dir()
    assert (base.parent / ("c" * 32)).is_symlink()


@pytest.mark.parametrize("root", ["", "/", "~"])
def test_unsafe_root(
    root: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prune, "run", Mock(side_effect=AssertionError))
    assert prune.main(["--output-root", root], "prune") == 2
    assert capsys.readouterr().err == "prune: unsafe output root\n"


def test_json_and_summary(
    base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = sum(path.lstat().st_blocks * 512 for path in [base, *base.rglob("*")])
    assert invoke(base, "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["freed_bytes"] == expected
    assert report["bases"] == [
        {
            "base": str(base),
            "workspace": str(tmp_path / "workspace"),
            "action": "pruned",
            "reason": "gone",
            "size_bytes": expected,
        }
    ]
    journal = tmp_path / "home/.local/state/tbhbdev/bazel-prune.jsonl"
    assert json.loads(journal.read_text()) == report
    assert invoke(base, "--json") == 0
    assert len(journal.read_text().splitlines()) == 2


def test_registered() -> None:
    assert alpha.COMMANDS["bazel-prune"] is prune.main
    assert "\n  bazel-prune " in alpha.USAGE


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_defaults(platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(prune.getpass, "getuser", lambda: "tester")
    expected = (
        "/private/var/tmp/_bazel_tester"
        if platform == "darwin"
        else str(prune.Path.home() / ".cache/bazel/_bazel_tester")
    )
    assert str(prune.default_root()) == expected


@pytest.mark.parametrize(
    ("days", "expected"), [(14, "idle"), (15, "fresh"), (16, "fresh")]
)
def test_idle_boundary(
    base: Path,
    days: int,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = base.parents[1] / "workspace"
    workspace.mkdir()
    age(base)
    now = base.stat().st_mtime + 15 * 86400
    monkeypatch.setattr(prune.time, "time", lambda: now)
    assert prune.selection(base, workspace, now - days * 86400) == expected
    assert invoke(base, "--dry-run", "--max-idle-days", str(days)) == 0


@pytest.mark.parametrize("name", ["server", "action_cache", "command.log", "."])
def test_newest_activity(base: Path, name: str) -> None:
    (base / "action_cache").mkdir()
    (base / "command.log").touch()
    age(base)
    os.utime(base / name, (9999999999, 9999999999))
    assert prune.newest_mtime(base) == 9999999999


@pytest.mark.parametrize("marker", [b" \n", b"\xff", b"missing"])
def test_bad_workspace(
    base: Path,
    marker: bytes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (base / "DO_NOT_BUILD_HERE").write_bytes(marker)
    if marker == b"missing":
        original = prune.Path.read_text

        def read_text(path: Path) -> str:
            if path.name == "DO_NOT_BUILD_HERE":
                raise PermissionError
            return original(path)

        monkeypatch.setattr(prune.Path, "read_text", read_text)
    size = prune.disk_size(base)
    assert invoke(base, "--json") == 0
    assert base.exists()
    reason = "empty-workspace" if marker == b" \n" else "unreadable-workspace"
    assert json.loads(capsys.readouterr().out)["bases"] == [
        {
            "base": str(base),
            "workspace": "",
            "action": "keep",
            "reason": reason,
            "size_bytes": size,
        }
    ]


def test_size_links(base: Path) -> None:
    (base / "hard-link").hardlink_to(base / "data")
    (base / "symbolic").symlink_to(base / "data")
    entries = [
        base,
        base / "data",
        base / "symbolic",
        base / "server",
        base / "DO_NOT_BUILD_HERE",
    ]
    assert prune.disk_size(base) == sum(p.lstat().st_blocks * 512 for p in entries)


@pytest.mark.parametrize("pid", ["oops", "", "0", "-1", "123"])
def test_pid_parsing(base: Path, pid: str) -> None:
    (base / "server/server.pid.txt").write_text(pid)
    assert prune.server_pid(base) == (int(pid) if pid in {"0", "-1", "123"} else 0)


@pytest.mark.parametrize("kind", ["outside", "symlink"])
def test_delete_rejects_escape(base: Path, kind: str) -> None:
    if kind == "symlink":
        link = base.parent / "link"
        link.symlink_to(base, target_is_directory=True)
        candidate, root = link, base.parent
    else:
        candidate, root = base, base.parent / "other"
    with pytest.raises(OSError, match="unsafe output base") as error:
        prune.delete_base(candidate, root)
    assert error.value.args == ("unsafe output base",)
    assert base.exists()


@pytest.mark.parametrize("fault", [OSError, subprocess.CalledProcessError])
def test_delete_failure(
    base: Path,
    fault: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_base: Path, _root: Path) -> None:
        if fault is OSError:
            raise OSError
        raise subprocess.CalledProcessError(1, "chmod")

    monkeypatch.setattr(prune, "delete_base", fail)
    assert invoke(base, "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["freed_bytes"] == 0
    assert report["bases"][0]["action"] == "prune"
    assert report["bases"][0]["reason"] == "delete-failed"
    assert base.exists()


def test_missing_root(base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert prune.main(["--output-root", str(base / "absent"), "--json"], "prune") == 0
    assert json.loads(capsys.readouterr().out) == {
        "bases": [],
        "dry_run": False,
        "freed_bytes": 0,
    }


def test_negative_days(base: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert invoke(base, "--max-idle-days", "-1") == 2
    assert capsys.readouterr().err == "prune: --max-idle-days must be nonnegative\n"


@pytest.mark.parametrize("flag", ["--bogus", "--max-idle-days=oops"])
def test_usage_errors(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        prune.main([flag], "prune")
    assert error.value.code == 2


def test_io_error(
    base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_path: Path) -> int:
        msg = "denied"
        raise PermissionError(msg)

    monkeypatch.setattr(prune, "disk_size", fail)
    assert invoke(base) == 1
    assert capsys.readouterr().err == "prune: denied\n"


def test_new_process_during_shutdown(
    base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = iter(["gone", "busy"])

    def select(_base: Path, _workspace: Path, _cutoff: float) -> str:
        return next(calls)

    monkeypatch.setattr(prune, "selection", select)
    assert invoke(base) == 0
    assert "keep busy " in capsys.readouterr().out
    assert base.exists()


@pytest.mark.parametrize("workspace_exists", [False, True])
@pytest.mark.parametrize("failure", [None, "os", "timeout"])
def test_shutdown_contract(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_exists: bool,
    failure: str | None,
) -> None:
    workspace = base.parents[1] / "workspace"
    if workspace_exists:
        workspace.mkdir()
    searched: list[str] = []

    def which(name: str) -> str | None:
        searched.append(name)
        return "/fake/bazelisk" if name == "bazelisk" else None

    monkeypatch.setattr(prune.shutil, "which", which)

    def run(argv: list[str], **kwargs: object) -> None:
        assert argv == ["/fake/bazelisk", f"--output_base={base}", "shutdown"]
        assert kwargs["timeout"] == 60
        assert kwargs["check"] is False
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        cwd = kwargs["cwd"]
        assert isinstance(cwd, prune.Path)
        assert cwd.is_dir()
        assert (cwd == workspace) is workspace_exists
        if failure == "os":
            raise OSError
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(prune.subprocess, "run", run)
    assert prune.shutdown(base, workspace) is (failure is None)
    assert searched == ["bazelisk"]


def test_no_build_tool(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    searched: list[str] = []

    def which(name: str) -> None:
        searched.append(name)

    monkeypatch.setattr(prune.shutil, "which", which)
    assert not prune.shutdown(base, base)
    assert searched == ["bazelisk", "bazel"]


def test_base_cwd_and_sibling(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = base.parents[1] / "workspace"
    monkeypatch.setattr(prune, "processes", lambda: {1: base / "deep", 2: None})
    assert prune.selection(base, workspace, 0) == "busy"
    monkeypatch.setattr(
        prune, "processes", lambda: {1: base.with_name(base.name + "x")}
    )
    assert prune.selection(base, workspace, 0) == "gone"


def test_fresh_live_server(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = base.parents[1] / "workspace"
    workspace.mkdir()
    (base / "server/server.pid.txt").write_text("42")
    monkeypatch.setattr(prune, "processes", lambda: {42: base.parent})
    assert prune.selection(base, workspace, 0) == "server-alive"


def test_linux_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    for name in ("not-a-pid", "1", "2", "3", "4", "5", "6"):
        (proc / name).mkdir()
    (proc / "1/cwd").symlink_to(tmp_path)
    (proc / "2/cwd").symlink_to(tmp_path)
    (proc / "6/cwd").symlink_to(tmp_path)
    (proc / "5").rmdir()
    original_iter = prune.Path.iterdir
    original_stat = prune.Path.stat
    original_readlink = prune.Path.readlink

    def entries(path: Path) -> Iterator[Path]:
        if str(path) == "/proc":
            return iter(
                [proc / name for name in ("not-a-pid", "1", "2", "3", "4", "5", "6")]
            )
        return original_iter(path)

    def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        info = original_stat(path, follow_symlinks=follow_symlinks)
        if path == proc / "2":
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info

    def readlink(path: Path) -> Path:
        if path == proc / "3/cwd":
            raise PermissionError
        if path == proc / "4/cwd":
            raise ProcessLookupError
        return original_readlink(path)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(prune.Path, "iterdir", entries)
    monkeypatch.setattr(prune.Path, "stat", stat)
    monkeypatch.setattr(prune.Path, "readlink", readlink)
    assert prune.processes() == {1: tmp_path, 3: None, 6: tmp_path}


@pytest.mark.parametrize("failure", ["count", "used", "zero-used", "full", None])
def test_mac_processes(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    library = Mock()

    def list_process_ids(
        kind: int,
        uid: int,
        buffer: ctypes.Array[ctypes.c_int] | None,
        size: int,
    ) -> int:
        assert kind == 4
        assert uid == os.getuid()
        if buffer is None:
            assert size == 0
            return 0 if failure == "count" else 24
        assert size == 1048
        buffer[:6] = [0, -1, 1, 11, 12, 13]
        if failure == "used":
            return -1
        if failure == "zero-used":
            return 0
        return size if failure == "full" else 24

    def pid_info(
        pid: int,
        flavor: int,
        argument: int,
        buffer: ctypes.Array[ctypes.c_char],
        size: int,
    ) -> int:
        assert flavor == 9
        assert argument == 0
        assert size == 2352
        if pid != 13:
            buffer[152:157] = b"/work"
        return size if pid != 12 else 0

    library.proc_listpids = Mock(side_effect=list_process_ids)
    library.proc_pidinfo = Mock(side_effect=pid_info)
    loader = Mock(return_value=library)
    monkeypatch.setattr(prune.ctypes, "CDLL", loader)
    monkeypatch.setattr(sys, "platform", "darwin")
    if failure:
        with pytest.raises(OSError, match="local process") as error:
            prune.processes()
        message = (
            "cannot enumerate local processes"
            if failure == "count"
            else "incomplete local process list"
        )
        assert error.value.args == (message,)
    else:
        assert prune.processes() == {
            1: prune.Path("/work"),
            11: prune.Path("/work"),
            12: None,
            13: None,
        }
    loader.assert_called_once_with("/usr/lib/libproc.dylib")
    assert library.proc_listpids.argtypes == [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    assert library.proc_listpids.restype == ctypes.c_int
    expected_types = [ctypes.c_int] * 2 + [ctypes.c_uint64, ctypes.c_void_p]
    expected_types.append(ctypes.c_int)
    assert library.proc_pidinfo.argtypes == expected_types
    assert library.proc_pidinfo.restype == ctypes.c_int


def test_dry_run_live_server(
    base: Path,
    child: subprocess.Popen[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = base.parents[1] / "gone"
    (base / "DO_NOT_BUILD_HERE").write_text(f"  {workspace}\n")
    (base / "server/server.pid.txt").write_text(str(child.pid))
    shutdown = Mock(side_effect=AssertionError)
    monkeypatch.setattr(prune, "shutdown", shutdown)
    expected = prune.disk_size(base)
    assert invoke(base, "--dry-run") == 0
    assert capsys.readouterr().out == (
        f"would-prune gone {expected} {workspace}\nwould free {expected} bytes\n"
    )
    shutdown.assert_not_called()
    assert base.exists()


def test_readonly_nested_and_external_file(base: Path) -> None:
    outside = base.parent / "outside-file"
    outside.write_bytes(b"keep outside")
    before = outside.stat()
    (base / "file-link").symlink_to(outside)
    (base / "broken-link").symlink_to(base / "absent")
    nested = base / "nested"
    nested.mkdir()
    (nested / "readonly").write_bytes(b"cache")
    (nested / "readonly").chmod(0o400)
    nested.chmod(0o500)
    assert invoke(base) == 0
    assert outside.read_bytes() == b"keep outside"
    assert outside.stat().st_mode == before.st_mode
    assert not base.exists()


def test_failed_chmod(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = base.parents[1] / "bin"
    binary.mkdir()
    tool = binary / "chmod"
    tool.write_text("#!/bin/sh\nexit 1\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary))
    assert invoke(base) == 1
    assert base.exists()


def test_zero_days(base: Path) -> None:
    (base.parents[1] / "workspace").mkdir()
    age(base, 1)
    assert invoke(base, "--max-idle-days", "0") == 0
    assert not base.exists()


def test_default_idle_limit(
    base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (base.parents[1] / "workspace").mkdir()
    age(base)
    now = base.stat().st_mtime + 14 * 86400 + 1
    monkeypatch.setattr(prune.time, "time", lambda: now)
    assert invoke(base, "--dry-run") == 0
    assert "would-prune idle " in capsys.readouterr().out


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        prune.main(["--help"], "prune")
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert output.startswith("usage: prune [")
    assert "Delete abandoned or idle Bazel output bases" in output
    for option in ("--dry-run", "--max-idle-days", "--output-root", "--json"):
        assert option in output


def test_activity_after_missing_directory(base: Path) -> None:
    (base / "command.log").touch()
    age(base)
    os.utime(base / "command.log", (9999999999, 9999999999))
    assert not (base / "action_cache").exists()
    assert prune.newest_mtime(base) == 9999999999


def test_mac_single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    library = Mock()

    def list_process_ids(
        kind: int,
        uid: int,
        buffer: ctypes.Array[ctypes.c_int] | None,
        size: int,
    ) -> int:
        assert kind == 4
        assert uid == os.getuid()
        if buffer is not None:
            assert size == 1028
            buffer[0] = 1
        return 4

    def pid_info(
        pid: int,
        flavor: int,
        argument: int,
        buffer: ctypes.Array[ctypes.c_char],
        size: int,
    ) -> int:
        assert pid == 1
        assert flavor == 9
        assert argument == 0
        assert size == 2352
        buffer[152] = b"/"
        return size

    library.proc_listpids = Mock(side_effect=list_process_ids)
    library.proc_pidinfo = Mock(side_effect=pid_info)
    monkeypatch.setattr(prune.ctypes, "CDLL", Mock(return_value=library))
    assert prune.mac_processes() == {1: prune.Path("/")}


def test_default_output_root(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(prune, "default_root", lambda: base.parent)
    assert prune.main(["--dry-run", "--json"], "prune") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["bases"][0]["base"] == str(base)
    assert report["dry_run"] is True


def test_no_shutdown_without_live_server(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown = Mock(side_effect=AssertionError)
    monkeypatch.setattr(prune, "shutdown", shutdown)
    assert invoke(base) == 0
    shutdown.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux marks deleted process paths")
def test_deleted_workspace_child(
    base: Path,
    child: subprocess.Popen[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    (base.parents[1] / "workspace").rmdir()
    assert child.poll() is None
    assert invoke(base) == 0
    assert base.exists()
    assert "keep busy " in capsys.readouterr().out


def test_existing_directory_with_deleted_suffix(tmp_path: Path) -> None:
    directory = tmp_path / "workspace (deleted)"
    directory.mkdir()
    link = tmp_path / "cwd"
    link.symlink_to(directory, target_is_directory=True)
    assert prune.working_directory(link) == directory


def test_deleted_directory_marker(tmp_path: Path) -> None:
    directory = tmp_path / "removed"
    link = tmp_path / "cwd"
    link.symlink_to(str(directory) + " (deleted)", target_is_directory=True)
    assert prune.working_directory(link) == directory
