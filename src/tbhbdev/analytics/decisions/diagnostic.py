"""Whitelisted migration diagnostics and literal password sentinel matches."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_PHRASES = (
    ("permission denied", "permission denied "),
    ("connection refused", "connection refused "),
    ("syntax error", "syntax error "),
    ("fatal", "FATAL "),
    ("error", "ERROR "),
)


def diagnostics(output: str) -> tuple[str, ...]:
    last_five = "\n".join(output.removesuffix("\n").split("\n")[-5:])
    return tuple(_line(line) for line in last_five.rstrip("\n").split("\n"))


def _line(line: str) -> str:
    lower = line.lower()
    phrase = next((label for text, label in _PHRASES if text in lower), "")
    result = f"migration diagnostic: {phrase}[redacted output]"
    if "/" in line:
        result += " [path]"
    if re.search(r":[0-9]+", line):
        result += " [endpoint]"
    if re.search("password|secret|token|credential|authorization|key", lower):
        result += " [credential]"
    return result


def sentinels(output: str, passwords: Iterable[str]) -> tuple[str, ...]:
    return tuple(password for password in passwords if password in output)
