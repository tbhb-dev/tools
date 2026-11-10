"""Validate refreshed auth as data before replacing the launch source."""

from __future__ import annotations

import json
from datetime import datetime

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        message = "last_refresh must be a timestamp"
        raise TypeError(message)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        message = "last_refresh must include a timezone"
        raise ValueError(message)
    return parsed


def validate(before: str | None, after: str) -> str | None:
    try:
        source: Json = json.loads(before) if before is not None else {}
        refreshed: Json = json.loads(after)
        if not isinstance(source, dict) or not isinstance(refreshed, dict):
            return "auth must be a JSON object"
        if not refreshed.keys() <= source.keys():
            return "auth added top-level keys"
        if "tokens" in source:
            tokens = refreshed.get("tokens")
            if not isinstance(tokens, dict) or not all(
                isinstance(v, str) for v in tokens.values()
            ):
                return "tokens must be an object of strings"
        if "last_refresh" in refreshed:
            changed = timestamp(refreshed["last_refresh"])
            if "last_refresh" in source and changed < timestamp(source["last_refresh"]):
                return "last_refresh moved backwards"
    except ValueError, TypeError:
        return "auth contains invalid JSON or timestamp"
    return None
