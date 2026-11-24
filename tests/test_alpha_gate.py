"""Exercise the gate without using a host."""

# cspell:ignore capfdbinary
from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import gate

if TYPE_CHECKING:
    from typing import BinaryIO

HOST = "review-host"
SHA = "a" * 40
PROG = "tbhbdev alpha gate"
RUN = ["run", "gate", "checkout path", HOST]
WAIT = ["wait", "gate", HOST, "--sha", SHA]
SSH = ["ssh", "-o", "IdentityAgent=none", HOST]
Files = tuple[Path, Path, Path]
Capture = pytest.CaptureFixture[str]


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Files:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".local/state/tbhb"
    state = root / "gate" / f"gate-{HOST}"
    state.mkdir(parents=True)
    log = root / f"tools-gate-{HOST}.log"
    monkeypatch.setattr(gate, "sleep", Mock(side_effect=AssertionError))
    return state, log, log.with_suffix(".log.sha")


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.mark.parametrize("status", [0, 7])
def test_run(files: Files, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    state, log, result = files
    final = (log, result, state / "exit", state / "reason")
    for path in (*final, state / "pid", state / "heartbeat"):
        path.write_text("old\n")
    monkeypatch.setenv("SSH_AUTH_SOCK", "old socket")
    now = 0
    monkeypatch.setattr(gate, "time", lambda: now)
    monkeypatch.setattr(gate.subprocess, "check_output", Mock(return_value=SHA + "\n"))
    atomic = gate.atomic

    def publish(path: Path, value: str) -> None:
        if path == result:
            assert log.read_bytes() == b"PASSED remote test\n"
            assert (state / "exit").read_text() == f"{status}\n"
        atomic(path, value)

    monkeypatch.setattr(gate, "atomic", publish)
    git = ["git", "-C", "checkout path"]
    bundle = str(log.parent / "gate-bundles/gate.bundle")
    repo = "~/Code/github.com/tbhb-dev/tools"
    remote = (
        f"cd {repo} && git fetch origin && "
        "git fetch ~/gate.bundle HEAD:refs/gate/gate && "
        f'test "$(git rev-parse refs/gate/gate)" = {SHA} && '
        "git worktree add --detach .worktrees/gate refs/gate/gate && "
        "cd .worktrees/gate && bash -lc 'bazelisk test --config=ci //...'"
    )
    cleanup = (
        f"cd {repo} && {{ git worktree remove --force .worktrees/gate; "
        "git update-ref -d refs/gate/gate; }; rm -f ~/gate.bundle"
    )
    scp = ["scp", "-q", "-o", "IdentityAgent=none", bundle, f"{HOST}:gate.bundle"]
    bundle_create = [*git, "bundle", "create", bundle, "origin/main..HEAD"]
    commands = iter([bundle_create, scp, [*SSH, remote], [*SSH, cleanup]])

    def wait(timeout: int) -> int:
        nonlocal now
        assert 0 < timeout <= 10
        assert (state / "heartbeat").read_text() == f"{stamp(now)}\n"
        assert not log.exists()
        assert not result.exists()
        if now >= 50:
            return status
        now += timeout
        raise subprocess.TimeoutExpired(SSH, timeout)

    def start(
        argv: list[str], *, stdout: BinaryIO, stderr: int, env: dict[str, str] | None
    ) -> nullcontext[Mock]:
        assert argv == next(commands)
        assert (state / "pid").read_text() == f"{os.getpid()}\n"
        assert (state / "heartbeat").read_text() == f"{stamp(now)}\n"
        assert not any(path.exists() for path in final)
        assert stderr == subprocess.STDOUT
        if argv[0] in {"scp", "ssh"}:
            assert env is not None
            assert (env["SSH_AUTH_SOCK"], env["HOME"]) == ("", os.environ["HOME"])
        if argv[-1] == remote:
            stdout.write(b"PASSED remote test\n")
            return nullcontext(Mock(wait=wait))
        rc = 9 if argv[-1] == cleanup else 0
        return nullcontext(Mock(wait=Mock(return_value=rc)))

    monkeypatch.setattr(gate.subprocess, "Popen", start)
    assert gate.main(RUN, PROG) == status
    assert list(commands) == []
    assert result.read_text() == f"{SHA}\n"


@pytest.mark.parametrize("status", [8, 1])
def test_early_failure(
    files: Files, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    state, _, result = files
    monkeypatch.setattr(gate.subprocess, "check_output", Mock(return_value=SHA))
    process = nullcontext(Mock(wait=Mock(return_value=0)))
    scp = nullcontext(Mock(wait=Mock(return_value=8))) if status == 8 else OSError()
    popen = Mock(side_effect=[process, scp, process])
    monkeypatch.setattr(gate.subprocess, "Popen", popen)
    assert gate.main(RUN, PROG) == status
    assert [call.args[0][0] for call in popen.call_args_list] == ["git", "scp", "ssh"]
    assert (state / "exit").read_text() == f"{status}\n"
    assert (state / "reason").read_text() == "scp bundle failed\n"
    assert not result.exists()


@pytest.mark.parametrize("status", ["0", "7", None])
def test_wait_summary(
    files: Files, capfdbinary: pytest.CaptureFixture[bytes], status: str | None
) -> None:
    state, log, result = files
    result.write_text(f"{SHA}\n")
    lines = (
        b"  FAILED  \nTIMEOUT\nNO STATUS\nBuild did NOT\nBuild completed\n"
        b"FLAKY\n\xff PASSED\ntests pass\nExecuted"
    )
    recent = b"new PASSED\n" * 31
    expected = f"exit status: {status or 'unknown'}\n".encode()
    if status is not None:
        (state / "exit").write_text(status)
        prefix = b"old FAILED\n" * 40 + recent + b"ignore Executed\n"
        log.write_bytes(prefix + b"\x1b]0;x\x07\x1b[31m" + lines + b"\x1b[0m\x1bc")
        expected = recent + lines + b"\n" + expected
    assert gate.main(WAIT, PROG) == int(status != "0")
    assert capfdbinary.readouterr().out == expected


NO_PID = "no pid file (the gate never started or was removed)"
DEAD = "pid 12345 is dead and no result was written"
PID = b"12345"
Failure = tuple[bytes | None, type[Exception] | None, bytes | None, str]


@pytest.mark.parametrize(
    "case",
    [
        *[(pid, None, None, NO_PID) for pid in (None, b"bad")],
        (PID, OverflowError, None, NO_PID),
        (PID, ProcessLookupError, None, DEAD),
        (PID, ProcessLookupError, b"details", f"{DEAD}; exit: 8; reason: scp failed"),
        *[(PID, None, beat, "heartbeat stale (missing)") for beat in (None, b"bad")],
        (PID, None, stamp(9).encode(), "heartbeat stale (age 91s, limit 90 s)"),
    ],
)
def test_wait_failure(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: Capture, case: Failure
) -> None:
    pid, error, beat, message = case
    state = files[0]
    for name, data in (("pid", pid), ("heartbeat", beat)):
        if data is not None:
            (state / name).write_bytes(data)
    if beat == b"details":
        (state / "exit").write_text("8\n")
        (state / "reason").write_text("scp failed\n")
    monkeypatch.setattr(gate.os, "kill", Mock(side_effect=error))
    monkeypatch.setattr(gate, "time", lambda: 100)
    assert gate.main(WAIT, PROG) == 1
    assert capsys.readouterr().out == f"FAILED: {message}\n"


def test_sha_mismatch(files: Files, capsys: Capture) -> None:
    files[2].write_text("other\n")
    assert gate.main(WAIT, PROG) == 1
    assert (
        capsys.readouterr().out == f"FAILED: result is for sha other, expected {SHA}\n"
    )


def test_timeout(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: Capture
) -> None:
    state = files[0]
    (state / "pid").write_bytes(PID)
    (state / "heartbeat").write_text(stamp(0))
    sleeps: list[float] = []
    monkeypatch.setattr(gate.os, "kill", Mock(side_effect=PermissionError))
    monkeypatch.setattr(gate, "time", lambda: 90)
    monkeypatch.setattr(gate, "monotonic", lambda: sum(sleeps))
    monkeypatch.setattr(gate, "sleep", sleeps.append)
    assert gate.main([*WAIT, "--timeout", "32"], PROG) == 3
    assert sleeps == [15, 15, 2]
    assert capsys.readouterr().out == "FAILED: timeout after 32 s, gate still running\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["run"],
        [*WAIT, "--bad"],
        [*WAIT, "--timeout", "-1"],
        ["run", "a/b", "checkout", HOST],
        [*RUN, ".."],
        [*RUN[:-1], ""],
    ],
)
def test_usage(argv: list[str], capsys: Capture) -> None:
    assert gate.main(argv, PROG) == 2
    assert f"usage: {PROG}" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["-h"], ["run", "-h"], ["wait", "-h"]])
def test_help(argv: list[str], capsys: Capture) -> None:
    assert alpha.main(["gate", *argv], "tbhbdev alpha") == 0
    assert f"usage: {PROG}" in capsys.readouterr().out
