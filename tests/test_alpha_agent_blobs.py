"""Tests for the text views of a blob."""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import zipfile
import zlib

import pytest

from tbhbdev.alpha.agent import blobs

NAME = "example-hidden"


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _joined(raw: bytes) -> str:
    found, unscanned = blobs.views(raw)
    assert unscanned is None
    return "\n".join(found)


@pytest.mark.parametrize(
    "raw",
    [
        NAME.encode(),
        NAME.encode("utf-16"),
        NAME.encode("utf-16-le"),
        NAME.encode("utf-16-be"),
        NAME.encode("utf-32-le"),
        NAME.encode("utf-32-be"),
        b"\xff\xfe" + NAME.encode() + b"\xff",
        gzip.compress(NAME.encode()),
        zlib.compress(NAME.encode()),
        bz2.compress(NAME.encode()),
        lzma.compress(NAME.encode()),
        _zip({"inner.txt": NAME.encode()}),
        _zip({NAME: b"x"}),
        gzip.compress(zlib.compress(NAME.encode("utf-16"))),
        _zip({"a.gz": gzip.compress(NAME.encode())}),
    ],
    ids=[
        "utf-8",
        "utf-16-bom",
        "utf-16-le",
        "utf-16-be",
        "utf-32-le",
        "utf-32-be",
        "invalid-utf-8",
        "gzip",
        "zlib",
        "bzip2",
        "xz",
        "zip-member",
        "zip-member-name",
        "nested-gzip-zlib-utf-16",
        "zip-of-gzip",
    ],
)
def test_the_name_is_visible_in_some_view(raw: bytes) -> None:
    assert NAME in _joined(raw)


@pytest.mark.parametrize(
    ("raw", "binary"),
    [
        (b"plain text\n", False),
        ("café\n".encode(), False),
        (b"", False),
        (b"\x00\x01", True),
        (b"\xff\xfe", True),
        ("x".encode("utf-16"), True),
    ],
)
def test_is_binary(raw: bytes, binary: bool) -> None:
    assert blobs.is_binary(raw) is binary


@pytest.mark.parametrize(
    "raw",
    [
        b"\x1f\x8b not really gzip",
        b"BZh not really bzip2",
        b"\xfd7zXZ\x00 not really xz",
        b"x\x9c not really zlib",
        b"PK\x03\x04 not really a zip",
        b"x",
        b"x!",
    ],
)
def test_data_that_only_looks_like_a_container_reads_as_bytes(raw: bytes) -> None:
    found, unscanned = blobs.views(raw)
    assert unscanned is None
    assert found[0] == raw.decode(errors="replace")


def test_an_oversized_stream_is_reported_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blobs, "BYTE_LIMIT", 100)
    found, unscanned = blobs.views(gzip.compress(b"a" * 1000))
    assert unscanned == "expands past the scan size limit"
    assert found[0] == gzip.compress(b"a" * 1000).decode(errors="replace")


def test_members_that_add_up_past_the_limit_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blobs, "BYTE_LIMIT", 100)
    archive = _zip({"a": b"a" * 60, "b": b"b" * 60})
    _, unscanned = blobs.views(archive)
    assert unscanned == "expands past the scan size limit"


def test_nesting_past_the_limit_is_reported() -> None:
    raw = NAME.encode()
    for _ in range(blobs.DEPTH_LIMIT + 1):
        raw = gzip.compress(raw)
    _, unscanned = blobs.views(raw)
    assert unscanned == "nests containers too deeply"


def test_nesting_at_the_limit_is_read() -> None:
    raw = NAME.encode()
    for _ in range(blobs.DEPTH_LIMIT):
        raw = gzip.compress(raw)
    assert NAME in _joined(raw)


def test_too_many_members_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blobs, "MEMBER_LIMIT", 2)
    _, unscanned = blobs.views(_zip({"a": b"1", "b": b"2", "c": b"3"}))
    assert unscanned == "holds too many archive members"


def test_an_encrypted_member_is_reported() -> None:
    raw = bytearray(_zip({NAME: b"secret"}))
    raw[raw.index(b"PK\x01\x02") + 8] |= 1
    found, unscanned = blobs.views(bytes(raw))
    assert unscanned == "holds an encrypted archive member"
    assert NAME in "\n".join(found)
