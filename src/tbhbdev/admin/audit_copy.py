"""Send the local audit log to the receiver and gate the key read on it.

The receiver is described in ``audit_receiver``. A key call appends its
``before`` record, sends the backlog through that record, and reads the key only
once the receiver acknowledges it. The sender keeps a cached count and digest in
a state file under an exclusive lock, and checks each acknowledgment against the
local log before it trusts it.
"""

import fcntl
import json
import math
import os
import re
import stat
import subprocess  # nosec B404
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from tbhbdev.admin import audit_receiver
from tbhbdev.admin.audit import GENESIS, LOG_NAME, RefusedError

UNACKNOWLEDGED = "audit_copy_unacknowledged"
DIVERGED = "audit_copy_diverged"
DAMAGED = "audit_copy_damaged"
UNAVAILABLE = "credential_unavailable"
PHASE_SECONDS = 10.0
REQUEST_SECONDS = 10.0
CATCH_UP_LINES = 20
CATCH_UP_SECONDS = 30.0
ACK_LIMIT = 128
LOCK_POLL = 0.05
ACK = re.compile(rb"ack 1 (0|[1-9][0-9]*) ([0-9a-f]{64})\n")
DESTINATION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*")
TARGET_FILE = "copy.target"
CONFIG_FILE = "copy.conf"
KNOWN_HOSTS_FILE = "copy.known_hosts"
STATE_FILE = "copy-state"
LOCK_FILE = "copy.lock"
# The command line outranks the configuration file, so these hold whatever the
# file says. Each closes a way for ssh to accept a host key it was not pinned to.
SSH_OPTIONS = (
    "BatchMode=yes",
    "StrictHostKeyChecking=yes",
    "GlobalKnownHostsFile=/dev/null",
    "VerifyHostKeyDNS=no",
    "KnownHostsCommand=none",
    "UpdateHostKeys=no",
    "ControlPath=none",
    "GSSAPIAuthentication=no",
    "GSSAPIKeyExchange=no",
)

if TYPE_CHECKING:
    from pathlib import Path

type Transport = Callable[[str, bytes, float], tuple[int, bytes]]


class Copy(Protocol):
    """What ``request.call`` needs from the copy."""

    def configured(self) -> bool: ...

    def gate(self, position: int) -> None:
        """Return only once the receiver holds line POSITION; else raise."""

    def trail(self, position: int) -> None:
        """Try to send through POSITION; never raises."""

    def catch_up(self) -> str:
        """Send part of the backlog and describe where the receiver stands."""


class Unconfigured:
    """A runtime with no receiver: the key stays unreadable."""

    def configured(self) -> bool:
        return False

    def gate(self, position: int) -> None:  # noqa: ARG002
        raise RefusedError(UNAVAILABLE)

    def trail(self, position: int) -> None:
        """Nothing to send to."""

    def catch_up(self) -> str:
        raise RefusedError(UNACKNOWLEDGED)


@dataclass(frozen=True)
class Count:
    n: int
    digest: str


@dataclass(frozen=True)
class Step:
    count: int
    sent: bool


def digest(line: bytes) -> str:
    return audit_receiver.digest(line)


def owned_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()


def ssh_transport(directory: Path) -> Transport:
    """Run ``ssh -F`` against the pinned host key; one connection per request."""

    def run(command: str, data: bytes, timeout: float) -> tuple[int, bytes]:
        target = (directory / TARGET_FILE).read_text().strip()
        if not DESTINATION.fullmatch(target):
            raise OSError(target)
        argv = ["ssh", "-F", str(directory / CONFIG_FILE)]
        for option in (
            *SSH_OPTIONS,
            f"UserKnownHostsFile={directory / KNOWN_HOSTS_FILE}",
            f"ConnectTimeout={max(1, math.ceil(timeout))}",
        ):
            argv += ["-o", option]
        argv += [target, command]
        environment = {
            name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ
        }
        done = subprocess.run(  # nosec B603 -- fixed ssh options
            argv,
            input=data,
            capture_output=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
        return done.returncode, done.stdout

    return run


@dataclass
class AuditCopy:
    directory: Path
    transport: Transport
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def configured(self) -> bool:
        target = self.directory / TARGET_FILE
        return all(
            owned_file(self.directory / name)
            for name in (TARGET_FILE, CONFIG_FILE, KNOWN_HOSTS_FILE)
        ) and bool(DESTINATION.fullmatch(target.read_text().strip()))

    def gate(self, position: int) -> None:
        """Send the backlog through POSITION within the phase deadline."""
        try:
            self.phase(position, self.clock() + PHASE_SECONDS)
        except OSError:
            raise RefusedError(UNACKNOWLEDGED) from None

    def trail(self, position: int) -> None:
        try:
            self.phase(position, self.clock() + PHASE_SECONDS)
        except RefusedError, OSError:
            return

    def phase(self, through: int, deadline: float) -> None:
        with self.locked(deadline):
            while True:
                lines = self.lines()
                step = self.step(lines, through, deadline - self.clock())
                if step.count >= through:
                    return

    def catch_up(self) -> str:
        """Send up to a capped number of lines, a lock and request at a time."""
        started = self.clock()
        sent = 0
        try:
            while True:
                with self.locked(self.clock() + REQUEST_SECONDS):
                    lines = self.lines()
                    step = self.step(lines, len(lines), REQUEST_SECONDS)
                if step.count >= len(lines):
                    return f"receiver count {step.count}, nothing left to send"
                sent += step.sent
                if sent >= CATCH_UP_LINES or self.clock() - started >= CATCH_UP_SECONDS:
                    return f"receiver count {step.count}, stopped at the cap; run again"
        except OSError:
            raise RefusedError(UNACKNOWLEDGED) from None

    @contextmanager
    def locked(self, deadline: float) -> Generator[None]:
        """The sender's exclusive lock; waiting past DEADLINE is unacknowledged."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            self.directory / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self.clock() >= deadline:
                        raise RefusedError(UNACKNOWLEDGED) from None
                    self.sleep(LOCK_POLL)
            yield
        finally:
            os.close(descriptor)

    def lines(self) -> list[bytes]:
        """Each local line, line feed included; a tail without one is a fragment."""
        try:
            descriptor = os.open(self.directory / LOG_NAME, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                data = handle.read()
        finally:
            os.close(descriptor)
        *whole, tail = data.split(b"\n")
        return [part + b"\n" for part in whole] + ([tail] if tail else [])

    def cache(self, lines: list[bytes]) -> Count | None:
        """The cached count once it matches the local log; a mismatch clears it."""
        try:
            raw = json.loads((self.directory / STATE_FILE).read_text())
            cached = Count(raw["n"], raw["digest"])
        except OSError, ValueError, KeyError, TypeError:
            return self.clear()
        if type(cached.n) is not int or not isinstance(cached.digest, str):
            return self.clear()
        if cached.n < 0 or not self.matches(lines, cached):
            return self.clear()
        return cached

    @staticmethod
    def matches(lines: list[bytes], count: Count) -> bool:
        if count.n == 0:
            return count.digest == GENESIS
        return count.n <= len(lines) and digest(lines[count.n - 1]) == count.digest

    def clear(self) -> None:
        (self.directory / STATE_FILE).unlink(missing_ok=True)

    def save(self, count: Count) -> None:
        temporary = self.directory / f"{STATE_FILE}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(
                descriptor, json.dumps({"n": count.n, "digest": count.digest}).encode()
            )
        finally:
            os.close(descriptor)
        temporary.replace(self.directory / STATE_FILE)

    def ask(self, command: str, data: bytes, remaining: float) -> bytes:
        """One request; the reply is the acknowledgment's matched text."""
        timeout = min(remaining, REQUEST_SECONDS)
        if timeout <= 0:
            raise RefusedError(UNACKNOWLEDGED)
        try:
            code, output = self.transport(command, data, timeout)
        except OSError, subprocess.SubprocessError:
            raise RefusedError(UNACKNOWLEDGED) from None
        if code != 0 or len(output) > ACK_LIMIT or not ACK.fullmatch(output):
            raise RefusedError(UNACKNOWLEDGED)
        return output

    def confirm(self, reply: bytes, lines: list[bytes]) -> int:
        """Check the acknowledgment against the local log and cache it."""
        match = ACK.fullmatch(reply)
        assert match  # noqa: S101  # nosec B101 - ask returns only matched text
        count = Count(int(match.group(1)), match.group(2).decode())
        if not self.matches(lines, count):
            raise RefusedError(DIVERGED)
        self.save(count)
        return count.n

    def step(self, lines: list[bytes], through: int, remaining: float) -> Step:
        """One request that moves the receiver toward THROUGH, or none."""
        cached = self.cache(lines)
        if cached is None:
            return Step(
                self.confirm(self.ask("head", b"", remaining), lines), sent=False
            )
        if cached.n >= through:
            return Step(cached.n, sent=False)
        position = cached.n + 1
        line = lines[position - 1]
        try:
            parsed = self.parse(line)
        except audit_receiver.RejectedError:
            # A repair may already have moved the receiver past this line.
            count = self.confirm(self.ask("head", b"", remaining), lines)
            if count >= position:
                return Step(count, sent=False)
            raise RefusedError(DAMAGED) from None
        before = digest(lines[position - 2]) if position > 1 else GENESIS
        if parsed.get("prev") != before:
            raise RefusedError(DIVERGED)
        try:
            reply = self.ask(f"append {position}", line, remaining)
        except RefusedError:
            self.clear()
            raise
        return Step(self.confirm(reply, lines), sent=True)

    @staticmethod
    def parse(line: bytes) -> dict[str, object]:
        """LINE as the receiver reads a new one, before its ``prev`` check."""
        body = audit_receiver.structural(line)
        try:
            value = json.loads(
                body.decode("ascii"),
                object_pairs_hook=audit_receiver.no_duplicates,
                parse_constant=audit_receiver.refuse_constant,
            )
        except ValueError, RecursionError:
            raise audit_receiver.RejectedError from None
        if not isinstance(value, dict):
            raise audit_receiver.RejectedError
        return value
