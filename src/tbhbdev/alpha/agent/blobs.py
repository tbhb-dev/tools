"""The text views of a blob that the push scan reads.

A blob can carry a name in more than one encoding: UTF-8, UTF-16 or UTF-32
text, or any of those inside a gzip, zlib, bzip2, xz or zip container. This
module decodes each view it can. It never refuses a blob. A blob it can't read
completely comes back with the reason, and the caller lists it for the reviewer.
"""

from __future__ import annotations

import bz2
import io
import lzma
import zipfile
import zlib
from dataclasses import dataclass
from typing import Protocol

# The most decompressed bytes one blob may expand to, and the nesting and
# member counts the scan follows before it reports the blob as unscanned.
BYTE_LIMIT = 64 * 1024 * 1024
DEPTH_LIMIT = 3
MEMBER_LIMIT = 1000

_CORRUPT = (
    zlib.error,
    OSError,
    EOFError,
    lzma.LZMAError,
    ValueError,
    RuntimeError,
    NotImplementedError,
    zipfile.BadZipFile,
)
_UTF16_AND_32 = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


class _Decompressor(Protocol):
    def decompress(self, data: bytes, max_length: int = -1, /) -> bytes: ...


class Unreadable(Exception):  # noqa: N818
    """A blob holds data the scan can't read to the end."""


@dataclass
class _Budget:
    left: int

    def take(self, size: int) -> None:
        self.left -= size
        if self.left < 0:
            message = "expands past the scan size limit"
            raise Unreadable(message)


def is_binary(raw: bytes) -> bool:
    """RAW isn't plain UTF-8 text."""
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return b"\x00" in raw


def views(raw: bytes) -> tuple[list[str], str | None]:
    """Every text view of RAW, and why the scan couldn't read all of it."""
    found: list[str] = []
    try:
        _collect(raw, found, _Budget(BYTE_LIMIT), 0)
    except Unreadable as reason:
        return found, str(reason)
    return found, None


def _collect(raw: bytes, found: list[str], budget: _Budget, depth: int) -> None:
    found.append(raw.decode("utf-8", errors="replace"))
    if b"\x00" in raw:
        found.extend(raw.decode(codec, errors="replace") for codec in _UTF16_AND_32)
    members, names = _unpack(raw, budget)
    if members and depth >= DEPTH_LIMIT:
        message = "nests containers too deeply"
        raise Unreadable(message)
    found.extend(names)
    for member in members:
        _collect(member, found, budget, depth + 1)


def _unpack(raw: bytes, budget: _Budget) -> tuple[list[bytes], list[str]]:
    """The members and member names of a container, or nothing for other data."""
    try:
        if raw.startswith(b"PK\x03\x04"):
            return _unzip(raw, budget)
        stream = _stream(raw)
        if stream is None:
            return [], []
        data = stream.decompress(raw, BYTE_LIMIT + 1)
    except _CORRUPT:
        # Data that only looks like a container is read as the bytes it is.
        return [], []
    budget.take(len(data))
    return [data], []


def _stream(raw: bytes) -> _Decompressor | None:
    if raw.startswith(b"\x1f\x8b"):
        return zlib.decompressobj(wbits=31)
    if raw.startswith(b"BZh"):
        return bz2.BZ2Decompressor()
    if raw.startswith(b"\xfd7zXZ\x00"):
        return lzma.LZMADecompressor()
    if len(raw) > 1 and raw[:1] == b"x" and (raw[0] * 256 + raw[1]) % 31 == 0:
        return zlib.decompressobj()
    return None


def _unzip(raw: bytes, budget: _Budget) -> tuple[list[bytes], list[str]]:
    members: list[bytes] = []
    names: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > MEMBER_LIMIT:
            message = "holds too many archive members"
            raise Unreadable(message)
        for info in infos:
            names.append(info.filename)
            if info.flag_bits & 0x1:
                message = "holds an encrypted archive member"
                raise Unreadable(message)
            with archive.open(info) as member:
                data = member.read(BYTE_LIMIT + 1)
            budget.take(len(data))
            members.append(data)
    return members, names
