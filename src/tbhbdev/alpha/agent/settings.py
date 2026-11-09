"""Reading the agent settings from the user config.

This is the only module that reads config. It reads the `[agent]` table once
and hands it to `decisions/config.py`, which validates it.
"""

from __future__ import annotations

from tbhbdev import userconfig
from tbhbdev.alpha.agent.decisions import config
from tbhbdev.alpha.agent.model import Invalid, Settings


def load() -> Settings:
    """Read and validate the `[agent]` table, which has no deployment defaults.

    Raises:
        userconfig.ConfigError: The file or the table is missing or unreadable,
            or a key is missing or invalid. The message starts with the file's
            path.
    """
    try:
        table = userconfig.table("agent")
    except (userconfig.ConfigError, OSError, UnicodeError) as exc:
        message = f"{userconfig.path()}: config cannot be read: {exc}"
        raise userconfig.ConfigError(message) from exc
    if table is None:
        message = f"{userconfig.path()}: [agent] table is required"
        raise userconfig.ConfigError(message)
    try:
        return config.parse(table)
    except Invalid as exc:
        message = f"{userconfig.path()}: {exc}"
        raise userconfig.ConfigError(message) from exc
