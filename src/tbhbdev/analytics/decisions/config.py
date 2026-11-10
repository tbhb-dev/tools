"""Parse an already gathered analytics table without reading configuration."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from tbhbdev.analytics.model import Settings

if TYPE_CHECKING:
    from collections.abc import Mapping

_MAX_PORT = 65535


def parse(table: Mapping[str, object]) -> Settings:
    known = {field.name for field in fields(Settings)}
    for key in table:
        if key not in known:
            message = f"[analytics] {key} not recognized"
            raise ValueError(message)
    defaults = Settings()
    password = table.get("password_file")
    return Settings(
        host=_text(table.get("host", defaults.host), "host"),
        port=_port(table.get("port", defaults.port)),
        database=_text(table.get("database", defaults.database), "database"),
        user=_text(table.get("user", defaults.user), "user"),
        password_file=None if password is None else _text(password, "password_file"),
        owner_role=_text(table.get("owner_role", defaults.owner_role), "owner_role"),
        loader_role=_text(
            table.get("loader_role", defaults.loader_role), "loader_role"
        ),
        reader_role=_text(
            table.get("reader_role", defaults.reader_role), "reader_role"
        ),
        grafana_role=_text(
            table.get("grafana_role", defaults.grafana_role), "grafana_role"
        ),
    )


def _port(value: object) -> int:
    if type(value) is not int or not 0 < value <= _MAX_PORT:
        message = "[analytics] port must be an integer from 1 to 65535"
        raise ValueError(message)
    return value


def _text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        message = f"[analytics] {key} must be a non-blank string"
        raise ValueError(message)
    return value
