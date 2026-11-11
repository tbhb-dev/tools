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


# Codex 0.164.0 TokenData and AccountOAuthProvenance (including the custom
# id_token string serializer):
# https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/login/src/token_data.rs
# Optional fields may be absent or null. Reject unknown fields at every level.
type Schema = dict[str, str | Schema]
JSON_STRING = "string"
TOKEN_SCHEMA: Schema = {
    "id_token": JSON_STRING,
    "access_token": JSON_STRING,
    "refresh_token": JSON_STRING,
    "account_id": "optional string",
    "login_owner_id": "optional string",
    "oauth_provenance": {"issuer": "string", "client_id": "string"},
}


def json_type(value: Json) -> str:
    return {
        dict: "object",
        list: "array",
        str: "string",
        int: "number",
        float: "number",
        bool: "boolean",
        type(None): "null",
    }[type(value)]


def field(value: Json, expected: str | Schema, key: str) -> str | None:
    if isinstance(expected, dict):
        return None if value is None else fields(value, expected, key)
    if value is None and expected == "optional string":
        return None
    if not isinstance(value, str):
        return f"{key}: found {json_type(value)}; expected {expected}"
    return None


def fields(value: Json, schema: Schema, key: str) -> str | None:
    if not isinstance(value, dict):
        return f"{key}: found {json_type(value)}; expected object"
    for name, item in value.items():
        path = f"{key}.{name}"
        if name not in schema:
            return f"{path}: found {json_type(item)}; unexpected key"
        if reason := field(item, schema[name], path):
            return reason
    for name, expected in schema.items():
        if expected == "string" and name not in value:
            return f"{key}.{name}: found missing; expected string"
    return None


def refresh_time(source: dict[str, Json], refreshed: dict[str, Json]) -> str | None:
    if "last_refresh" not in refreshed:
        return None
    for document in (source, refreshed):
        if "last_refresh" in document:
            value = document["last_refresh"]
            try:
                timestamp(value)
            except ValueError, TypeError:
                return (
                    f"last_refresh: found {json_type(value)}; "
                    "expected timestamp with timezone"
                )
    if "last_refresh" in source and timestamp(refreshed["last_refresh"]) < timestamp(
        source["last_refresh"]
    ):
        return "last_refresh: found string; timestamp moved backwards"
    return None


def validate(before: str | None, after: str) -> str | None:
    try:
        source: Json = json.loads(before) if before is not None else {}
        refreshed: Json = json.loads(after)
    except ValueError, TypeError:
        return "auth: found invalid JSON"
    if not isinstance(source, dict):
        return f"auth.before: found {json_type(source)}; expected object"
    if not isinstance(refreshed, dict):
        return f"auth.after: found {json_type(refreshed)}; expected object"
    # AuthDotJson always serializes OPENAI_API_KEY, including null:
    # https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/login/src/auth/storage.rs#L49-L72
    # persist_tokens also sets last_refresh before saving:
    # https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/login/src/auth/manager.rs#L1698
    for key in refreshed.keys() - source.keys() - {"OPENAI_API_KEY", "last_refresh"}:
        return f"auth.{key}: found {json_type(refreshed[key])}; unexpected key"
    if "tokens" in source:
        reason = (
            fields(refreshed["tokens"], TOKEN_SCHEMA, "tokens")
            if "tokens" in refreshed
            else "tokens: found missing; expected object"
        )
        if reason:
            return reason
    return field(
        refreshed.get("OPENAI_API_KEY"), "optional string", "auth.OPENAI_API_KEY"
    ) or refresh_time(source, refreshed)
