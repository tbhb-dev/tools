"""Exercise the gate without using a host."""

# cspell:ignore capfdbinary
from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime
from hashlib import sha256
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


@pytest.mark.parametrize(("status", "repo"), [(0, []), (7, [""])])
def test_run(
    files: Files,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    repo: list[str],
) -> None:
    state, log, result = files
    for path in (
        log,
        result,
        *(state / name for name in ("exit", "reason", "pid", "heartbeat")),
    ):
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
    remote_repo = "~/Code/github.com/tbhb-dev/tools"
    remote = (
        f"cd {remote_repo} && git fetch -q origin && "
        "git fetch -q ~/gate.bundle HEAD:refs/gate/gate && "
        f'test "$(git rev-parse refs/gate/gate)" = {SHA} && '
        "git worktree add -q --detach .worktrees/gate refs/gate/gate && "
        "cd .worktrees/gate && bash -lc 'bazelisk test --config=ci //...'"
    )
    cleanup = (
        f"cd {remote_repo} && {{ git worktree remove --force .worktrees/gate; "
        "git update-ref -d refs/gate/gate; }; rm -f ~/gate.bundle"
    )
    scp = ["scp", "-q", "-o", "IdentityAgent=none", bundle, f"{HOST}:gate.bundle"]
    bundle_create = [*git, "bundle", "create", bundle, "origin/main..HEAD"]
    commands = iter([bundle_create, scp, [*SSH, remote], [*SSH, cleanup]])

    def wait(timeout: int) -> int:
        nonlocal now
        assert 0 < timeout <= 10
        assert (state / "heartbeat").read_text() == f"{stamp(now)}\n"
        assert log.read_text() == "old\n"
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
        assert not any((state / name).exists() for name in ("exit", "reason"))
        assert stderr == subprocess.STDOUT
        if argv[0] in {"scp", "ssh"}:
            assert env is not None
            assert (env["SSH_AUTH_SOCK"], env["HOME"]) == ("", os.environ["HOME"])
        if argv[-1] == remote:
            stdout.write(b"PASSED remote test\n")
            return nullcontext(Mock(wait=wait))
        if argv in (bundle_create, scp):
            assert result.read_text() == "old\n"
            assert gate.result_status(state, log, result, "old") is None
        rc = 9 if argv[-1] == cleanup else 0
        return nullcontext(Mock(wait=Mock(return_value=rc)))

    monkeypatch.setattr(gate.subprocess, "Popen", start)
    assert gate.main([*RUN, *repo], PROG) == status
    assert list(commands) == []
    assert result.read_text() == f"{SHA}\n"
    assert not (state / "superseded").exists()


@pytest.mark.parametrize(
    ("step", "status"), [("create bundle", 8), ("scp bundle", 8), ("scp bundle", 1)]
)
def test_early_failure(
    files: Files,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Capture,
    step: str,
    status: int,
) -> None:
    state, log, result = files
    log.write_bytes(b"previous FAILED\n")
    result.write_text(f"{SHA}\n")
    (state / "exit").write_text("0\n")
    monkeypatch.setattr(gate.subprocess, "check_output", Mock(return_value=SHA))
    process = nullcontext(Mock(wait=Mock(return_value=0)))
    failure = nullcontext(Mock(wait=Mock(return_value=8))) if status == 8 else OSError()
    steps = [failure] if step == "create bundle" else [process, failure, process]
    expected = ["git"] if step == "create bundle" else ["git", "scp", "ssh"]
    popen = Mock(side_effect=steps)
    monkeypatch.setattr(gate.subprocess, "Popen", popen)
    assert gate.main(RUN, PROG) == status
    assert [call.args[0][0] for call in popen.call_args_list] == expected
    assert (state / "exit").read_text() == f"{status}\n"
    assert (state / "reason").read_text() == f"{step} failed\n"
    assert sorted(path.name for path in state.iterdir()) == [
        "exit",
        "heartbeat",
        "pid",
        "reason",
        "superseded",
    ]
    assert log.read_bytes() == b"previous FAILED\n"
    assert result.read_text() == f"{SHA}\n"
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(gate.os, "kill", Mock(side_effect=ProcessLookupError))
    assert gate.main(WAIT, PROG) == 1
    assert capsys.readouterr().out == (
        f"FAILED: pid {os.getpid()} is dead and no result was written; "
        f"exit: {status}; reason: {step} failed\n"
    )


@pytest.mark.parametrize(
    ("remote_output", "summary"),
    [
        (b"PASSED\n", b""),
        (b"\x1b[31mBuild did NOT\x1b[0m", b"Build did NOT\n"),
        (
            (
                b"PASSED\n\x1b[31mtests pass\x1b[0m\nFAILED\nBuild did NOT\n"
                b"FAILED\ntests pass\nFAILED\n"
            ),
            b"tests pass\nFAILED\nBuild did NOT\nFAILED\ntests pass\n",
        ),
    ],
)
def test_run_summary(
    files: Files,
    monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes],
    remote_output: bytes,
    summary: bytes,
) -> None:
    monkeypatch.setattr(gate.subprocess, "check_output", Mock(return_value=SHA))

    def run_step(
        command: list[str], _directory: Path, output: BinaryIO, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if "bazelisk test" in command[-1]:
            output.write(remote_output)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate, "run_step", run_step)
    assert gate.main(RUN, PROG) == 0
    assert files[1].read_bytes() == remote_output
    assert capfdbinary.readouterr().out == summary


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
            path = state / name
            path.write_bytes(data)
            os.utime(path, (0, 0))
    if beat == b"details":
        (state / "exit").write_text("8\n")
        (state / "reason").write_text("scp failed\n")
    monkeypatch.setattr(gate.os, "kill", Mock(side_effect=error))
    monkeypatch.setattr(gate, "time", lambda: 100)
    assert gate.main(WAIT, PROG) == 1
    assert capsys.readouterr().out == f"FAILED: {message}\n"


@pytest.mark.parametrize("status", [None, "0", "7"])
def test_wait_checks_again_when_pid_dies(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: Capture, status: str | None
) -> None:
    state, _, result = files
    (state / "pid").write_bytes(PID)

    def process_exits(pid: int, signal: int) -> None:
        assert (pid, signal) == (int(PID), 0)
        assert not result.exists()
        if status is not None:
            (state / "exit").write_text(status)
            result.write_text(f"{SHA}\n")
        raise ProcessLookupError

    monkeypatch.setattr(gate.os, "kill", process_exits)
    assert gate.main(WAIT, PROG) == int(status != "0")
    expected = f"FAILED: {DEAD}" if status is None else f"exit status: {status}"
    assert capsys.readouterr().out == f"{expected}\n"


@pytest.mark.parametrize("beat", [None, b"bad", b"\xff", b"2026-01-01"])
def test_wait_heartbeat_startup_grace(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: Capture, beat: bytes | None
) -> None:
    state, _, _ = files
    pid = state / "pid"
    pid.write_bytes(b"1")
    os.utime(pid, (100, 100))
    if beat is not None:
        (state / "heartbeat").write_bytes(beat)
    monkeypatch.setattr(gate.os, "kill", Mock(return_value=None))
    monkeypatch.setattr(gate, "time", lambda: 104)
    assert gate.main([*WAIT, "--timeout", "0"], PROG) == 3
    assert capsys.readouterr().out == "FAILED: timeout after 0 s, gate still running\n"
    monkeypatch.setattr(gate, "time", lambda: 105)
    assert gate.main(WAIT, PROG) == 1
    assert capsys.readouterr().out == "FAILED: heartbeat stale (missing)\n"


def test_wait_pid_file_disappears_during_startup(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: Capture
) -> None:
    pid = files[0] / "pid"
    pid.write_bytes(PID)

    def remove_pid(_pid: int, _signal: int) -> None:
        pid.unlink()

    monkeypatch.setattr(gate.os, "kill", remove_pid)
    assert gate.main(WAIT, PROG) == 1
    assert capsys.readouterr().out == "FAILED: heartbeat stale (missing)\n"


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


@pytest.mark.usefixtures("files")
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "the following arguments are required: command"),
        (["run"], "the following arguments are required: NAME, WORKTREE, HOST"),
        (["run", "--hel", *RUN[1:]], "unrecognized arguments: --hel"),
        (WAIT[:3], "the following arguments are required: --sha"),
        ([*WAIT, "--time", "1"], "unrecognized arguments: --time 1"),
        ([*WAIT, "--timeout", "-1"], "--timeout must be nonnegative"),
        (["run", "a/b", "checkout", HOST], "invalid NAME"),
        ([*RUN, ".."], "invalid REPO"),
        ([*RUN[:-1], ""], "invalid HOST"),
    ],
)
def test_usage(argv: list[str], message: str, capsys: Capture) -> None:
    assert gate.main(argv, PROG) == 2
    assert capsys.readouterr().err.endswith(f"error: {message}\n")


@pytest.mark.usefixtures("files")
@pytest.mark.parametrize(
    "host",
    [
        "-review-host",
        "-agent@review-host",
        "agent@-review-host",
        "agent name@host",
        "agent@review host",
        "agent@host\n",
        "agent@host;cmd",
        "agent$(cmd)@host",
        "agent@other@host",
        "@host",
        "agent@",
        ".@host",
        "..@host",
        "agent@..",
        "agent@../host",
    ],
)
def test_invalid_host(host: str, capsys: Capture) -> None:
    assert gate.main(["run", "gate", "checkout", "--", host], PROG) == 2
    assert f"{PROG}: error: invalid HOST\n" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("agent@review-host", f"ssh.{sha256(b'gate/agent@review-host').hexdigest()}"),
        ("Agent@review-host", f"ssh.{sha256(b'gate/Agent@review-host').hexdigest()}"),
        ("agent-at-review-host", "gate-agent-at-review-host"),
    ],
)
def test_host_paths(
    files: Files, monkeypatch: pytest.MonkeyPatch, host: str, key: str
) -> None:
    run = Mock(return_value=5)
    wait = Mock(return_value=6)
    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "wait", wait)
    assert gate.main([*RUN[:-1], host, "other-repo"], PROG) == 5
    assert gate.main(["wait", "gate", host, "--sha", SHA], PROG) == 6
    root = files[1].parent
    log = root / f"tools-{key}.log"
    paths = (root / "gate" / key, log, log.with_suffix(".log.sha"))
    assert run.call_args.args[1:] == wait.call_args.args[1:] == paths
    assert run.call_args.args[0].host == wait.call_args.args[0].host == host
    assert run.call_args.args[0].repo == "other-repo"
    assert wait.call_args.args[0].timeout == 14400


@pytest.mark.parametrize(
    ("argv", "usage", "text"),
    [
        (
            ["-h"],
            "[-h] {run,wait} ...",
            (
                "Run a Bazel gate on a host and wait for a specific commit's result.",
                "run tests",
                "wait for a result",
            ),
        ),
        (["run", "-h"], "run [-h] NAME WORKTREE HOST [REPO]", ()),
        (["wait", "-h"], "wait [-h] --sha SHA [--timeout SECONDS] NAME HOST", ()),
    ],
)
def test_help(
    argv: list[str], usage: str, text: tuple[str, ...], capsys: Capture
) -> None:
    assert alpha.main(["gate", *argv], "tbhbdev alpha") == 0
    output = capsys.readouterr().out
    assert output.startswith(f"usage: {PROG} {usage}\n")
    assert all(line in output for line in text)
