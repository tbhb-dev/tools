"""Plugins: subcommands and config files from other installed packages.

A package adds to tbhbdev through an entry point in the `tbhbdev.plugins`
group. The entry point resolves to an object, usually a module, that may
expose either or both of:

- `TOOLS: dict[str, Tool]`, subcommands to add beside the built-in ones. A
  name that a built-in command or another plugin already uses is an error,
  never a silent override.
- `config(name: str) -> str | None`, the text of the config file NAME, or
  None when the plugin doesn't supply it. The package doesn't include a
  config file of its own, so a plugin's file is the only one there is.

Plugins load on first use, not at import. A plugin that fails to load is
reported on stderr once and skipped, so the built-in commands keep working.
"""

from __future__ import annotations

import functools
import sys
from importlib.metadata import entry_points
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from tbhbdev.cli import Tool

GROUP = "tbhbdev.plugins"


class PluginError(Exception):
    """Two providers claim the same command or config file."""


@functools.cache
def load() -> tuple[tuple[str, object], ...]:
    """Each installed plugin's entry point name and object, in entry point order.

    A plugin whose entry point fails to load is reported and left out.
    """
    loaded: list[tuple[str, object]] = []
    for point in entry_points(group=GROUP):
        try:
            loaded.append((point.name, point.load()))
        # Loading runs the plugin's module code, which can raise anything.
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"tbhbdev: cannot load plugin {point.name!r}: "
                f"{type(exc).__name__}: {exc}\n"
            )
    return tuple(loaded)


def tools(builtins: Mapping[str, Tool]) -> dict[str, Tool]:
    """BUILTINS with every plugin's TOOLS added.

    Raises:
        PluginError: A plugin's command has a name already taken.
    """
    merged = dict(builtins)
    owner = dict.fromkeys(builtins, "the built-in commands")
    for name, plugin in load():
        extra: Mapping[str, Tool] = getattr(plugin, "TOOLS", {})
        for command, tool in extra.items():
            if command in owner:
                message = (
                    f"plugin {name!r} defines the command {command!r}, "
                    f"which {owner[command]} already define"
                )
                raise PluginError(message)
            owner[command] = f"plugin {name!r}"
            merged[command] = tool
    return merged


def config(name: str) -> str:
    """The text of the config file NAME: a plugin's, or else the package's.

    Raises:
        PluginError: More than one plugin supplies NAME.
    """
    supplied: list[tuple[str, str]] = []
    for plugin_name, plugin in load():
        reader: Callable[[str], str | None] | None = getattr(plugin, "config", None)
        text = reader(name) if reader is not None else None
        if text is not None:
            supplied.append((plugin_name, text))
    if len(supplied) > 1:
        names = ", ".join(repr(n) for n, _ in supplied)
        message = f"plugins {names} each supply {name}"
        raise PluginError(message)
    if supplied:
        return supplied[0][1]
    # Equivalent mutants: files(None) finds this module's package too, and
    # macOS's case-insensitive disk finds config/ under any spelling.
    default = files("tbhbdev").joinpath("config", name)  # pragma: no mutate
    # Equivalent mutants: every spelling of UTF-8, and the hosts' default
    # encoding, read the file alike.
    return default.read_text(encoding="utf-8")  # pragma: no mutate
