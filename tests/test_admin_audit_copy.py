"""The sender against the real receiver module as its transport."""

import fcntl
import io
import json
import os
import subprocess
from contextlib import AbstractContextManager  # noqa: TC003
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003
from typing import cast

import pytest

from tbhbdev.admin import audit_copy, audit_receiver
from tbhbdev.admin.audit import GENESIS, Audit, RefusedError
from tbhbdev.admin.audit_copy import AuditCopy, Unconfigured

SEGMENT = 70000
QUOTA = 400000


@dataclass
class Link:
    """A transport that runs the receiver in-process and can break on demand."""

    receiver: Path
    commands: list[str] = field(default_factory=list)
    fail: dict[str, str] = field(default_factory=dict)
    override: bytes | None = None

    def __call__(self, command: str, data: bytes, timeout: float) -> tuple[int, bytes]:
        self.commands.append(command)
        assert timeout > 0
        kind = self.fail.get(command.partition(" ")[0]) or self.fail.get(command)
        if kind == "oserror":
            raise OSError
        if kind == "timeout":
            raise subprocess.TimeoutExpired(["ssh"], timeout)
        if kind == "exit":
            return 255, b""
        out = io.BytesIO()
        code = audit_receiver.serve(
            str(self.receiver), SEGMENT, QUOTA, command, io.BytesIO(data), out
        )
        return code, self.override if self.override is not None else out.getvalue()


@dataclass
class Clock:
    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class Rig:
    directory: Path
    link: Link
    clock: Clock
    copy: AuditCopy
    audit: Audit

    def log(self, count: int) -> list[int]:
        return [self.audit.append({"n": number}) for number in range(count)]

    def lines(self) -> list[bytes]:
        return (self.directory / "calls.jsonl").read_bytes().splitlines(keepends=True)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    receiver = tmp_path / "receiver"
    receiver.mkdir(mode=0o700)
    (receiver / "segment-1.jsonl").write_bytes(b"")
    (receiver / "head").write_text(f"1 0 {GENESIS} 0\n")
    directory = tmp_path / "admin"
    link = Link(receiver)
    clock = Clock()
    copy = AuditCopy(directory, link, clock, clock.sleep)
    return Rig(directory, link, clock, copy, Audit(directory))


def received(rig: Rig) -> bytes:
    return (rig.link.receiver / "segment-1.jsonl").read_bytes()


def state(rig: Rig) -> dict[str, object]:
    return cast(
        "dict[str, object]", json.loads((rig.directory / "copy-state").read_text())
    )


def test_the_gate_sends_the_backlog_through_the_line_in_order(rig: Rig) -> None:
    positions = rig.log(3)
    rig.copy.gate(positions[-1])
    assert received(rig) == b"".join(rig.lines())
    assert rig.link.commands == ["head", "append 1", "append 2", "append 3"]
    assert state(rig) == {"n": 3, "digest": audit_copy.digest(rig.lines()[2])}


def test_a_cached_count_skips_the_head_request(rig: Rig) -> None:
    rig.log(2)
    rig.copy.gate(2)
    rig.link.commands.clear()
    position = rig.audit.append({"n": 2})
    rig.copy.gate(position)
    assert rig.link.commands == ["append 3"]


def test_a_gate_with_nothing_unsent_makes_no_request_after_the_cache(
    rig: Rig,
) -> None:
    rig.log(1)
    rig.copy.gate(1)
    rig.link.commands.clear()
    rig.copy.gate(1)
    assert rig.link.commands == []


def test_a_fresh_empty_log_and_receiver_need_only_a_head(rig: Rig) -> None:
    rig.copy.phase(0, rig.clock() + 10)
    assert rig.link.commands == ["head"]
    assert state(rig) == {"n": 0, "digest": GENESIS}


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"n": 1}',
        '{"n": -1, "digest": "' + GENESIS + '"}',
        '{"n": "1", "digest": "' + GENESIS + '"}',
        '{"n": 0, "digest": 5}',
        '{"n": 0, "digest": "' + "1" * 64 + '"}',
        '{"n": 5, "digest": "' + GENESIS + '"}',
        '{"n": 1, "digest": "' + GENESIS + '"}',
    ],
)
def test_a_cache_that_fails_its_check_is_cleared(rig: Rig, text: str) -> None:
    rig.log(1)
    rig.directory.mkdir(exist_ok=True)
    (rig.directory / "copy-state").write_text(text)
    rig.copy.gate(1)
    assert rig.link.commands[0] == "head"
    assert state(rig)["n"] == 1


def test_a_replaced_log_that_still_matches_the_receiver_at_n_is_diverged(
    rig: Rig,
) -> None:
    rig.log(2)
    rig.copy.gate(2)
    (rig.directory / "calls.jsonl").unlink()
    (rig.directory / "copy-state").unlink()
    Audit(rig.directory).append({"other": True})
    with pytest.raises(RefusedError, match=r"^audit_copy_diverged$"):
        rig.copy.gate(1)


def test_a_receiver_line_that_differs_from_the_local_one_is_diverged(
    rig: Rig,
) -> None:
    rig.log(2)
    rig.copy.gate(2)
    rig.lines()
    local = (rig.directory / "calls.jsonl").read_bytes()
    (rig.directory / "calls.jsonl").write_bytes(local.replace(b'"n": 1', b'"n": 9'))
    (rig.directory / "copy-state").unlink()
    with pytest.raises(RefusedError, match=r"^audit_copy_diverged$"):
        rig.copy.gate(2)


def test_a_line_whose_prev_is_wrong_is_diverged(rig: Rig) -> None:
    rig.log(2)
    local = (rig.directory / "calls.jsonl").read_bytes()
    (rig.directory / "calls.jsonl").write_bytes(
        local.replace(GENESIS.encode(), b"1" * 64, 1)
    )
    with pytest.raises(RefusedError, match=r"^audit_copy_diverged$"):
        rig.copy.gate(2)
    assert rig.link.commands == ["head"]


def test_a_fragment_is_damaged_unless_the_receiver_already_passed_it(
    rig: Rig,
) -> None:
    rig.log(1)
    path = rig.directory / "calls.jsonl"
    path.write_bytes(path.read_bytes() + b'{"partial')
    position = rig.audit.append({"n": 2})
    assert position == 3
    with pytest.raises(RefusedError, match=r"^audit_copy_damaged$"):
        rig.copy.gate(position)
    assert rig.link.commands == ["head", "append 1", "head"]
    # An operator repair moves the receiver past the fragment.
    fragment = rig.lines()[1]
    head = audit_copy.digest(rig.lines()[0])
    receiver = rig.link.receiver
    (receiver / "segment-1.jsonl").write_bytes(rig.lines()[0] + fragment)
    (receiver / "head").write_text(
        f"1 2 {audit_copy.digest(fragment)} {len(rig.lines()[0] + fragment)}\n"
    )
    assert head
    rig.copy.gate(position)
    assert received(rig) == b"".join(rig.lines())


def test_a_line_over_the_size_limit_is_damaged(rig: Rig) -> None:
    path = rig.directory
    path.mkdir()
    (path / "calls.jsonl").write_bytes(b"x" * 70000 + b"\n")
    with pytest.raises(RefusedError, match=r"^audit_copy_damaged$"):
        rig.copy.gate(1)


@pytest.mark.parametrize("kind", ["oserror", "timeout", "exit"])
@pytest.mark.parametrize("command", ["head", "append"])
def test_a_failed_request_refuses_and_a_failed_append_clears_the_cache(
    rig: Rig, kind: str, command: str
) -> None:
    rig.log(2)
    if command == "append":
        rig.copy.gate(1)
        assert (rig.directory / "copy-state").exists()
    rig.link.fail[command] = kind
    with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
        rig.copy.gate(2)
    assert (rig.directory / "copy-state").exists() is (command == "head" and False)


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"ack 1 0 " + b"0" * 64,
        b"ack 2 0 " + b"0" * 64 + b"\n",
        b"ack 1 00 " + b"0" * 64 + b"\n",
        b"ack 1 0 " + b"A" * 64 + b"\n",
        b"ack 1 0 " + b"0" * 64 + b"\nx",
        b"ack 1 0 " + b"0" * 64 + b"\n" + b" " * 128,
    ],
)
def test_a_malformed_acknowledgment_is_unacknowledged(rig: Rig, reply: bytes) -> None:
    rig.log(1)
    rig.link.override = reply
    with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
        rig.copy.gate(1)


def test_an_acknowledgment_of_a_count_past_the_local_log_is_diverged(rig: Rig) -> None:
    rig.log(1)
    rig.link.override = b"ack 1 5 " + b"1" * 64 + b"\n"
    with pytest.raises(RefusedError, match=r"^audit_copy_diverged$"):
        rig.copy.gate(1)


def test_a_phase_with_no_time_left_sends_nothing(rig: Rig) -> None:
    rig.log(1)
    rig.clock.now = 100.0
    with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
        rig.copy.phase(1, 100.0)
    assert rig.link.commands == []


def test_a_held_lock_waits_then_refuses_at_the_deadline(rig: Rig) -> None:
    rig.log(1)
    rig.directory.mkdir(exist_ok=True)
    holder = os.open(rig.directory / "copy.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
            rig.copy.gate(1)
    finally:
        os.close(holder)
    assert sum(rig.clock.slept) >= audit_copy.PHASE_SECONDS
    assert rig.link.commands == []


def test_a_lock_released_during_the_wait_is_taken(rig: Rig) -> None:
    rig.log(1)
    rig.directory.mkdir(exist_ok=True)
    holder = os.open(rig.directory / "copy.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)

    def release(seconds: float) -> None:
        rig.clock.sleep(seconds)
        os.close(holder)

    rig.copy.sleep = release
    rig.copy.gate(1)
    assert rig.link.commands[-1] == "append 1"


def test_a_state_directory_that_cannot_be_made_is_unacknowledged(
    rig: Rig, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    copy = AuditCopy(blocker / "sub", rig.link, rig.clock, rig.clock.sleep)
    with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
        copy.gate(1)


def test_a_trailing_send_never_raises(rig: Rig) -> None:
    rig.log(1)
    rig.link.fail["head"] = "exit"
    rig.copy.trail(1)
    rig.link.fail.clear()
    rig.copy.trail(1)
    assert received(rig) == b"".join(rig.lines())
    blocker = rig.directory / "file"
    blocker.write_text("x")
    AuditCopy(blocker / "sub", rig.link).trail(1)


def test_a_head_that_moved_the_receiver_forward_ends_the_phase_early(
    rig: Rig,
) -> None:
    rig.log(2)
    rig.copy.gate(2)
    (rig.directory / "copy-state").unlink()
    rig.link.commands.clear()
    rig.copy.gate(2)
    assert rig.link.commands == ["head"]


def test_catch_up_drains_a_small_backlog_and_reports_the_count(rig: Rig) -> None:
    rig.log(3)
    message = rig.copy.catch_up()
    assert message == "receiver count 3, nothing left to send"
    assert received(rig) == b"".join(rig.lines())


def test_catch_up_stops_at_twenty_lines_and_resumes(rig: Rig) -> None:
    rig.log(25)
    first = rig.copy.catch_up()
    assert first == "receiver count 20, stopped at the cap; run again"
    assert rig.link.commands.count("head") == 1
    assert rig.copy.catch_up() == "receiver count 25, nothing left to send"


def test_catch_up_stops_at_thirty_seconds(rig: Rig) -> None:
    rig.log(5)
    sent = rig.link

    def slow(command: str, data: bytes, timeout: float) -> tuple[int, bytes]:
        rig.clock.now += 20
        return sent(command, data, timeout)

    rig.copy.transport = slow
    message = rig.copy.catch_up()
    assert message == "receiver count 1, stopped at the cap; run again"


def test_catch_up_counts_a_head_request_as_no_line(rig: Rig) -> None:
    rig.log(1)
    rig.link.fail.clear()
    assert rig.copy.catch_up() == "receiver count 1, nothing left to send"


def test_catch_up_refuses_on_a_failed_request_and_a_busy_lock(rig: Rig) -> None:
    rig.log(1)
    rig.link.fail["head"] = "exit"
    with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
        rig.copy.catch_up()
    blocker = rig.directory / "file"
    blocker.write_text("x")
    with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
        AuditCopy(blocker / "sub", rig.link).catch_up()


def test_catch_up_takes_the_lock_again_for_each_request(rig: Rig) -> None:
    rig.log(2)
    taken: list[int] = []
    original = AuditCopy.locked

    def spy(self: AuditCopy, deadline: float) -> AbstractContextManager[None]:
        taken.append(1)
        return original(self, deadline)

    AuditCopy.locked = spy  # type: ignore[method-assign]
    try:
        rig.copy.catch_up()
    finally:
        AuditCopy.locked = original  # type: ignore[method-assign]
    assert len(taken) == 3


def test_the_local_read_ends_a_tail_without_a_line_feed(rig: Rig) -> None:
    rig.directory.mkdir()
    assert rig.copy.lines() == []
    (rig.directory / "calls.jsonl").write_bytes(b'{"a": 1}\n{"b"')
    assert rig.copy.lines() == [b'{"a": 1}\n', b'{"b"']


def test_parse_accepts_only_one_json_object() -> None:
    assert AuditCopy.parse(b'{"prev": "x"}\n') == {"prev": "x"}
    for line in (b"[]\n", b"nope\n", b'{"a": 1, "a": 2}\n', b'{"a": NaN}\n', b"{}"):
        with pytest.raises(audit_receiver.RejectedError):
            AuditCopy.parse(line)


def test_configured_needs_three_owned_files_and_a_valid_destination(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "copy"
    directory.mkdir()
    copy = AuditCopy(directory, lambda *_: (0, b""))
    assert not copy.configured()
    for name in ("copy.conf", "copy.known_hosts"):
        (directory / name).write_text("x")
    (directory / "copy.target").write_text("-oProxyCommand=x@host\n")
    assert not copy.configured()
    (directory / "copy.target").write_text("account@receiver.example\n")
    assert copy.configured()
    (directory / "copy.conf").unlink()
    (directory / "copy.conf").symlink_to(directory / "copy.known_hosts")
    assert not copy.configured()


def test_the_unconfigured_copy_refuses_the_gate_and_ignores_the_trail() -> None:
    copy = Unconfigured()
    assert not copy.configured()
    with pytest.raises(RefusedError, match=r"^credential_unavailable$"):
        copy.gate(1)
    assert copy.trail(1) is None
    with pytest.raises(RefusedError, match=r"^audit_copy_unacknowledged$"):
        copy.catch_up()


def test_ssh_runs_with_the_pinned_options_and_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "copy.target").write_text("account@receiver.example\n")
    seen: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, b"out", b"err")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("HOME", "/home/x")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/agent")
    result = audit_copy.ssh_transport(tmp_path)("append 7", b"line\n", 2.2)
    assert result == (0, b"out")
    argv = seen["argv"]
    assert argv[:3] == ["ssh", "-F", str(tmp_path / "copy.conf")]  # type: ignore[index]
    assert argv[-2:] == ["account@receiver.example", "append 7"]  # type: ignore[index]
    options = [argv[i + 1] for i, a in enumerate(argv) if a == "-o"]  # type: ignore[arg-type,index]
    assert options[:-2] == list(audit_copy.SSH_OPTIONS)
    assert options[-2] == f"UserKnownHostsFile={tmp_path / 'copy.known_hosts'}"
    assert options[-1] == "ConnectTimeout=3"
    assert seen["env"] == {"PATH": "/bin", "HOME": "/home/x"}
    assert seen["input"] == b"line\n"
    assert seen["timeout"] == 2.2
    monkeypatch.delenv("HOME")
    audit_copy.ssh_transport(tmp_path)("head", b"", 0.2)
    assert seen["env"] == {"PATH": "/bin"}
    assert "ConnectTimeout=1" in seen["argv"]  # type: ignore[operator]


def test_ssh_refuses_a_destination_that_could_be_an_option(
    tmp_path: Path,
) -> None:
    (tmp_path / "copy.target").write_text("-oProxyCommand=x\n")
    with pytest.raises(OSError):  # noqa: PT011
        audit_copy.ssh_transport(tmp_path)("head", b"", 1.0)
