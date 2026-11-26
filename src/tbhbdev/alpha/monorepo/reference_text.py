"""Preserve source positions while excluding comments and manifest metadata."""

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import PurePosixPath

METADATA = frozenset({"repository", "homepage", "documentation", "issues"})
GO_FILES = frozenset({"go.mod", "go.sum", "go.work", "go.work.sum"})
DOUBLE = rb'"(?:[^"\\\r\n]|\\[\s\S])*"'
SINGLE = rb"'(?:[^'\\\r\n]|\\[\s\S])*'"
TRIPLE = rb'"""(?:\\[\s\S]|(?!""")[^\\])*"""'
TOML_LITERAL = rb"'''[\s\S]*?'''{1,3}"
STARLARK_SINGLE = rb"'''(?:\\[\s\S]|(?!''')[^\\])*'''"
C_COMMENT = rb"(?P<comment>//[^\r\n]*|/\*[\s\S]*?(?:\*/|\Z))"
HASH_COMMENT = rb"(?P<comment>\#[^\r\n]*)"
JSON_STRINGS = DOUBLE + b"|" + SINGLE
TOML_STRING = TRIPLE + rb'"{0,2}|' + TOML_LITERAL + b"|" + DOUBLE + rb"|'[^'\r\n]*'"
JSON_TOKENS = re.compile(JSON_STRINGS + rb"|[{}\[\],]")
TOML_TOKENS = re.compile(TOML_STRING + rb"|[{}\[\],\n]")
JSON_COMMENTS = re.compile(JSON_STRINGS + b"|" + C_COMMENT)
TOML_COMMENTS = re.compile(TOML_STRING + b"|" + HASH_COMMENT)
STARLARK_COMMENTS = re.compile(
    TRIPLE + b"|" + STARLARK_SINGLE + b"|" + JSON_STRINGS + b"|" + HASH_COMMENT
)
GO_COMMENTS = re.compile(rb"`[^`]*`|" + JSON_STRINGS + b"|" + C_COMMENT)
JSON_KEY = re.compile(rb"\s*(" + DOUBLE + rb")\s*:")
TOML_KEY = rb"(?:" + DOUBLE + rb"|'[^'\r\n]*'|[\w-]+)"
TOML_HEADER = re.compile(rb"[ \t]*\[([^\r\n]+)\][ \t\r]*$")
TOML_ASSIGN = re.compile(rb"\s*(" + TOML_KEY + rb"(?:\s*\.\s*" + TOML_KEY + rb")*)\s*=")
TOML_KEYS = re.compile(TOML_KEY)
PYPROJECT_METADATA_TABLES: frozenset[tuple[str, ...]] = frozenset(
    {
        (),
        ("project",),
        ("project", "urls"),
        ("tool", "poetry"),
        ("tool", "poetry", "urls"),
    }
)
CARGO_METADATA_TABLES: frozenset[tuple[str, ...]] = frozenset(
    {(), ("package",), ("workspace", "package")}
)
NON_NEWLINE = re.compile(rb"[^\r\n]")


def blank(data: bytes) -> bytes:
    return NON_NEWLINE.sub(b" ", data)


def blank_comment(match: re.Match[bytes]) -> bytes:
    return blank(match[0]) if match["comment"] is not None else match[0]


def remove_comments(path: PurePosixPath, data: bytes) -> bytes:
    tokens: re.Pattern[bytes]
    if path.suffix in {".json5", ".jsonc"}:
        tokens = JSON_COMMENTS
    elif path.suffix == ".toml":
        tokens = TOML_COMMENTS
    elif path.suffix in {".bazel", ".bzl"} or path.name in {"BUILD", "WORKSPACE"}:
        tokens = STARLARK_COMMENTS
    elif path.suffix == ".go" or path.name in GO_FILES:
        tokens = GO_COMMENTS
    else:
        return data
    return tokens.sub(blank_comment, data)


def sections(
    data: bytes, tokens: re.Pattern[bytes], separator: bytes
) -> Iterator[tuple[int, int]]:
    """Split on delimiters outside strings and nested values."""
    start = depth = 0
    for match in tokens.finditer(data):
        token = match[0]
        if token in {b"[", b"{"}:
            depth += 1
        elif token in {b"]", b"}"}:
            depth -= 1
        elif token == separator and depth == 0:
            yield start, match.start()
            start = match.end()
    yield start, len(data)


def json_metadata(data: bytes) -> Iterator[tuple[int, int]]:
    start = len(data) - len(data.lstrip())
    if data[start : start + 1] != b"{":
        return
    start += 1
    for left, right in sections(data[start : data.rfind(b"}")], JSON_TOKENS, b","):
        if key := JSON_KEY.match(data, start + left, start + right):
            try:
                name = json.loads(key[1])
            except ValueError, UnicodeError:
                continue
            if name in METADATA:
                yield key.end(), start + right


def toml_keys(data: bytes) -> tuple[str, ...]:
    return tuple(
        k.strip(b"'\"").decode(errors="replace") for k in TOML_KEYS.findall(data)
    )


def toml_value_spans(
    statement: bytes,
    table: tuple[str, ...],
    parents: frozenset[tuple[str, ...]],
    offset: int,
) -> Iterator[tuple[int, int]]:
    key = TOML_ASSIGN.match(statement)
    if key is None:
        return
    parts = table + toml_keys(key[1])
    if any(
        parts[:index] in parents and part.lower() in METADATA
        for index, part in enumerate(parts)
    ):
        yield offset + key.end(), offset + len(statement)
        return
    value = statement[key.end() :].lstrip()
    if value.startswith(b"{") and any(
        parent[: len(parts)] == parts for parent in parents
    ):
        start = len(statement) - len(value) + 1
        for left, right in sections(value[1 : value.rfind(b"}")], TOML_TOKENS, b","):
            yield from toml_value_spans(
                statement[start + left : start + right],
                parts,
                parents,
                offset + start + left,
            )


def toml_metadata(data: bytes, name: str) -> Iterator[tuple[int, int]]:
    parents = (
        PYPROJECT_METADATA_TABLES if name == "pyproject.toml" else CARGO_METADATA_TABLES
    )
    table: tuple[str, ...] = ()
    for left, right in sections(data, TOML_TOKENS, b"\n"):
        statement = data[left:right]
        if header := TOML_HEADER.match(statement):
            table = toml_keys(header[1])
        else:
            yield from toml_value_spans(statement, table, parents, left)


def scan_text(path: PurePosixPath, data: bytes) -> bytes:
    data = remove_comments(path, data)
    if path.name == "package.json":
        spans = json_metadata(data)
    elif path.name in {"pyproject.toml", "Cargo.toml"}:
        spans = toml_metadata(data, path.name)
    else:
        return data
    masked = bytearray(data)
    for start, end in spans:
        masked[start:end] = blank(data[start:end])
    return bytes(masked)
