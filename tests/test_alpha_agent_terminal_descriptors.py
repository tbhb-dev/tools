"""Inherited terminal descriptors remain observable behind a private /dev."""

import errno
import json
import os
import shutil
import subprocess
from contextlib import nullcontext
from typing import Never
lazy from pathlib import Path

import agent_live_host_probe as probe
import pytest


@pytest.mark.parametrize("descriptor", [0, 1, 2])
def test_inherited_terminal_descriptor_records_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor: int
) -> None:
    calls: dict[int, list[bytes]] = {fd: [] for fd in range(3)}
    closed: list[int] = []

    def missing(_path: str, _flags: int) -> Never:
        raise FileNotFoundError(errno.ENOENT, "fixture private device namespace")

    def ioctl(fd: int, operation: int, value: bytes) -> None:
        assert operation == probe.termios.TIOCSTI
        calls[fd].append(value)
        if fd != descriptor:
            raise OSError(errno.EPERM, "fixture refusal")

    monkeypatch.setattr(probe.os, "open", missing)
    monkeypatch.setattr(probe.os, "close", closed.append)
    monkeypatch.setattr(probe.fcntl, "ioctl", ioctl)
    witness = tmp_path / "witness.json"
    probe.inject_terminal(witness, "/dev/pts/fixture")
    assert json.loads(witness.read_text()) == {
        "descriptor": "/dev/pts/fixture",
        "attempted": False,
        "error": errno.ENOENT,
        "inherited": [
            {
                "descriptor": fd,
                "attempted": True,
                "error": 0 if fd == descriptor else errno.EPERM,
            }
            for fd in range(3)
        ],
    }
    assert b"".join(calls[descriptor]) == probe.INPUT_MARKER
    assert all(
        calls[fd] == [probe.INPUT_MARKER[:1]] for fd in calls if fd != descriptor
    )
    assert closed == []


@pytest.mark.parametrize("descriptor", [None, 0, 1, 2])
def test_legacy_input_enabled_still_rejects_every_successful_inherited_injection(
    descriptor: int | None,
) -> None:
    attempts = [
        {"descriptor": fd, "attempted": True, "error": 0 if fd == descriptor else 1}
        for fd in range(3)
    ]
    statuses = probe.inherited_terminal_statuses(
        probe.INPUT_MARKER, b"", attempts, kernel_blocked=False
    )
    assert statuses == {
        fd: "injected" if fd == descriptor else "blocked" for fd in range(3)
    }


def test_inherited_kernel_refusal_passes_without_injection_control() -> None:
    assert probe.inherited_terminal_statuses(
        b"",
        b"",
        [
            {"descriptor": fd, "attempted": True, "error": errno.EPERM}
            for fd in range(3)
        ],
        kernel_blocked=True,
    ) == dict.fromkeys(range(3), "blocked by kernel")


def test_inherited_observations_require_all_three_descriptors() -> None:
    with pytest.raises(AssertionError):
        probe.inherited_terminal_statuses(b"", b"", [], kernel_blocked=True)


def test_missing_hard_link_target_records_every_refusal(tmp_path: Path) -> None:
    ln = shutil.which("ln")
    assert ln is not None
    assert probe.hard_link_attempt(tmp_path / "absent", tmp_path / "link", ln) == {
        "link": 1,
        "read": errno.ENOENT,
        "write": errno.ENOENT,
        "chmod": errno.ENOENT,
    }
    witness = tmp_path / "witness.json"
    probe.main("hardlinks", [str(witness), str(tmp_path), ln, str(tmp_path / "absent")])
    assert set(json.loads(witness.read_text())["absent"]) == {
        "link",
        "read",
        "write",
        "chmod",
    }


def test_terminal_timeout_still_stops_the_created_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[tuple[int, int]] = []

    class Child:
        pid = 12345

        def communicate(self, *, timeout: int) -> Never:
            assert timeout == 45
            command = "fixture"
            raise subprocess.TimeoutExpired(command, timeout)

        def wait(self, *, timeout: int) -> int:
            assert timeout == 5
            return 124

    def start(*_args: object, **_kwargs: object) -> nullcontext[Child]:
        return nullcontext(Child())

    def stop(group: int, number: int) -> None:
        stopped.append((group, number))

    monkeypatch.setattr(probe.subprocess, "Popen", start)
    monkeypatch.setattr(probe.os, "killpg", stop)
    assert probe.observe_terminal(("fixture",), {"PATH": os.defpath}, tmp_path) == (
        probe.TerminalObservation(b"", 124)
    )
    assert stopped == [(12345, probe.signal.SIGKILL)]
