"""Read analytics settings once from the shared user configuration."""

import os

from tbhbdev import userconfig
from tbhbdev.analytics.decisions import config
lazy from tbhbdev.analytics.model import Settings


def load() -> Settings:
    table = dict(userconfig.optional_table("analytics") or {})
    for key, variable in (
        ("host", "PGHOST"),
        ("port", "PGPORT"),
        ("database", "PGDATABASE"),
        ("user", "PGUSER"),
    ):
        if variable in os.environ:
            value = os.environ[variable]
            table[key] = int(value) if key == "port" else value
    result = config.parse(table)
    if result.user != result.owner_role:
        message = "analytics requires the owner role as the connection user"
        raise ValueError(message)
    return result
