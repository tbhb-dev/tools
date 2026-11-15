"""Receive the administrator audit log off the host that wrote it.

Run it as an ssh forced command: ``python -I audit_receiver.py DIR SEGMENT QUOTA``,
where DIR holds the receiver's files, SEGMENT is the segment size in bytes and
QUOTA the total size of all segments. The module imports only the standard
library, so the receiver host needs nothing else. It reads one request from
``SSH_ORIGINAL_COMMAND``, either ``head`` or ``append K``, and prints
``ack 1 <n> <digest>`` once the line it names is durable. The digest names
line n, the newest line, whatever K was. A replay of the newest line (K equal to
n) succeeds only when the line hashes to that digest. A replay of an older line
(K below n) checks only the line's form, because the receiver keeps no index of
older digests, so a sender compares the acknowledged digest with its own line n.

Every digest is the SHA-256 of a line's bytes without its final line feed. The
directory holds numbered segments, a ``lock`` file and a ``head`` file of one
line, ``<segment> <n> <digest> <length>``: the current segment, the count of
committed lines, the digest of the last one, and the segment's byte length up
to it. Each request runs under an exclusive lock and first returns the
directory to what the head file names, so an acknowledgment covers only
durable history.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import BinaryIO

GENESIS = "0" * 64
MAX_LINE = 65536
MIN_SEGMENT = MAX_LINE
ARGUMENTS = 3
FIRST_BYTE = 32
LAST_BYTE = 126
PROTOCOL = 1
PRIVATE = 0o600
SEGMENT = re.compile(r"segment-([1-9][0-9]*)\.jsonl")
NUMBER = r"(?:0|[1-9][0-9]*)"
HEAD = re.compile(rf"([1-9][0-9]*) ({NUMBER}) ([0-9a-f]{{64}}) ({NUMBER})\n")
APPEND = re.compile(r"append ([1-9][0-9]*)")
HEX64 = re.compile(r"[0-9a-f]{64}")


class RejectedError(Exception):
    """A request the receiver refuses; it exits nonzero with empty output."""


def digest(line: bytes) -> str:
    """The SHA-256 of LINE without its final line feed."""
    return hashlib.sha256(line.removesuffix(b"\n")).hexdigest()


def segment_name(number: int) -> str:
    return f"segment-{number}.jsonl"


def open_regular(path: str, flags: int) -> int:
    """Open PATH as a private regular file, never through a symbolic link."""
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, PRIVATE)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        message = "not a regular file"
        raise RejectedError(message)
    try:
        os.fchmod(descriptor, PRIVATE)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def sync_directory(directory: str) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_head(directory: str) -> tuple[int, int, str, int]:
    """The segment, count, digest and length the head file records."""
    try:
        descriptor = open_regular(str(Path(directory) / "head"), os.O_RDONLY)
        try:
            text = os.read(descriptor, 512).decode("ascii")
        finally:
            os.close(descriptor)
    except OSError, UnicodeDecodeError:
        message = "no readable head file"
        raise RejectedError(message) from None
    match = HEAD.fullmatch(text)
    if not match:
        message = "malformed head file"
        raise RejectedError(message)
    segment, count, last, length = match.groups()
    return int(segment), int(count), last, int(length)


def commit_head(directory: str, head: tuple[int, int, str, int]) -> None:
    """Replace the head file: temporary name, sync, rename, directory sync."""
    temporary = str(Path(directory) / "head.tmp")
    descriptor = open_regular(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        data = "{} {} {} {}\n".format(*head).encode("ascii")
        if os.write(descriptor, data) != len(data):
            message = "short write"
            raise RejectedError(message)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    Path(temporary).replace(Path(directory) / "head")
    sync_directory(directory)


def segments(directory: str) -> dict[int, str]:
    """Each segment number in DIRECTORY with its path."""
    found: dict[int, str] = {}
    for entry in Path(directory).iterdir():
        match = SEGMENT.fullmatch(entry.name)
        if match:
            found[int(match.group(1))] = str(entry)
    return found


def recover(directory: str, head: tuple[int, int, str, int]) -> None:
    """Return the directory to what the head file names."""
    current, _count, _last, length = head
    path = str(Path(directory) / segment_name(current))
    try:
        descriptor = open_regular(path, os.O_WRONLY)
    except OSError:
        message = "the current segment is missing"
        raise RejectedError(message) from None
    try:
        size = os.fstat(descriptor).st_size
        if size < length:
            message = "the current segment is shorter than its head"
            raise RejectedError(message)
        if size > length:
            os.ftruncate(descriptor, length)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for number, other in segments(directory).items():
        if number > current:
            Path(other).unlink()
    leftover = str(Path(directory) / "head.tmp")
    Path(leftover).unlink(missing_ok=True)
    sync_directory(directory)


def structural(line: bytes) -> bytes:
    """LINE's bytes before the final line feed, when LINE is one valid line."""
    if len(line) > MAX_LINE or not line.endswith(b"\n"):
        message = "not one line within the size limit"
        raise RejectedError(message)
    body = line[:-1]
    if any(not FIRST_BYTE <= byte <= LAST_BYTE for byte in body):
        message = "a byte outside 32 through 126"
        raise RejectedError(message)
    return body


def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        message = "a duplicate member name"
        raise RejectedError(message)
    return dict(pairs)


def refuse_constant(_name: str) -> object:
    message = "NaN and Infinity are not JSON"
    raise RejectedError(message)


def validate(line: bytes, last: str) -> None:
    """Refuse LINE unless it is one JSON object whose ``prev`` is LAST."""
    body = structural(line)
    try:
        value = json.loads(
            body.decode("ascii"),
            object_pairs_hook=no_duplicates,
            parse_constant=refuse_constant,
        )
    except ValueError, RecursionError:
        message = "not JSON"
        raise RejectedError(message) from None
    prev = value.get("prev") if isinstance(value, dict) else None
    if not (isinstance(prev, str) and HEX64.fullmatch(prev) and prev == last):
        message = "prev is not the head digest"
        raise RejectedError(message)


def used(directory: str) -> int:
    """The total size of every segment."""
    return sum(Path(path).lstat().st_size for path in segments(directory).values())


def append(
    directory: str, head: tuple[int, int, str, int], line: bytes, quota: int, size: int
) -> tuple[int, int, str, int]:
    """Write LINE after the head's line and return the new head."""
    current, count, _last, length = head
    if used(directory) + len(line) > quota:
        message = "the quota is full"
        raise RejectedError(message)
    if length + len(line) > size:
        current += 1
        length = 0
        descriptor = open_regular(
            str(Path(directory) / segment_name(current)),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        )
        os.close(descriptor)
        sync_directory(directory)
    descriptor = open_regular(
        str(Path(directory) / segment_name(current)), os.O_WRONLY | os.O_APPEND
    )
    try:
        if os.write(descriptor, line) != len(line):
            message = "short write"
            raise RejectedError(message)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    new = (current, count + 1, digest(line), length + len(line))
    commit_head(directory, new)
    return new


def acknowledge(directory: str, head: tuple[int, int, str, int]) -> bytes:
    """Sync the head file and the directory, then the acknowledgment text."""
    descriptor = open_regular(str(Path(directory) / "head"), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    sync_directory(directory)
    return f"ack {PROTOCOL} {head[1]} {head[2]}\n".encode("ascii")


def handle(
    directory: str,
    size: int,
    quota: int,
    command: str | None,
    stdin: BinaryIO,
) -> bytes:
    """The reply to COMMAND, with its input read from STDIN; raises on refusal."""
    request = None if command is None else APPEND.fullmatch(command)
    if command != "head" and not request:
        message = "not head or append K"
        raise RejectedError(message)
    data = stdin.read(MAX_LINE + 1)
    lock = open_regular(str(Path(directory) / "lock"), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        head = read_head(directory)
        recover(directory, head)
        if request is None:
            if data:
                message = "head takes no input"
                raise RejectedError(message)
            return acknowledge(directory, head)
        position = int(request.group(1))
        if position > head[1] + 1:
            message = "a position past the next line"
            raise RejectedError(message)
        if position <= head[1]:
            body = structural(data)
            if position == head[1] and digest(body) != head[2]:
                message = "the replayed line is not the head's line"
                raise RejectedError(message)
            return acknowledge(directory, head)
        validate(data, head[2])
        return acknowledge(directory, append(directory, head, data, quota, size))
    finally:
        os.close(lock)


def serve(  # noqa: PLR0913, PLR0917 - one request, all of its inputs
    directory: str,
    size: int,
    quota: int,
    command: str | None,
    stdin: BinaryIO,
    stdout: BinaryIO,
) -> int:
    """Answer one request; the exit status is 0 only with an acknowledgment."""
    try:
        reply = handle(directory, size, quota, command, stdin)
    except RejectedError, OSError:
        return 1
    stdout.write(reply)
    stdout.flush()
    return 0


def main(
    argv: list[str], command: str | None, stdin: BinaryIO, stdout: BinaryIO
) -> int:
    """Check the directory, segment size and quota, then answer COMMAND."""
    if len(argv) != ARGUMENTS or not all(
        value.isascii() and value.isdecimal() for value in argv[1:]
    ):
        return 1
    size, quota = int(argv[1]), int(argv[2])
    if size < MIN_SEGMENT or quota < 2 * size:
        return 1
    return serve(argv[0], size, quota, command, stdin, stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(
        main(
            sys.argv[1:],
            os.environ.get("SSH_ORIGINAL_COMMAND"),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    )
