"""The user's tbhbdev config: the values of one deployment, from one file.

The file is `tbhbdev/config.toml` in `$XDG_CONFIG_HOME`, which defaults to
`.config` in the home directory. It names the 1Password account, the Connect
server and endpoints, the agent hosts' tag, the vaults and the owners whose
private repositories the gitleaks config covers. The code has no
defaults for any of them: a missing file, key or value fails with a message
that names the file and the `[table] key`.

The file is read when a value is first needed, so a command that needs none,
such as `--help`, works without it. `render` writes the file back out with one
value set, for the copy `tbhbdev host bootstrap` installs on each agent host.

The optional `[harness]` table configures the user-level Claude Code hooks that
`tbhbdev alpha install` writes. Every key is a string or a list of strings,
since `render` keeps nothing else. A missing file, table or key is not an error
for these hooks: checks that need a setting do nothing without it. Sign-off
and claim checks run without configuration.

    rule          text `alpha state-guard` prints at session start
    decisions_dir path its block messages cite as the place to check first
    open_note     path suffix of the note it holds to the item rule
    hosts         `["name=role", ...]`, which `alpha session-context` lists;
                  a name alone, with no `=`, has no role
"""

from __future__ import annotations

import os
import re
import tomllib
from json.encoder import encode_basestring
from pathlib import Path


class ConfigError(Exception):
    """The config file is missing, isn't TOML, or lacks a value."""


def path() -> Path:
    """The config file in `$XDG_CONFIG_HOME`, or in `.config` under `$HOME`.

    An empty `XDG_CONFIG_HOME` counts as unset, as in the shell's `:-`.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "tbhbdev" / "config.toml"


def _load(wanted: str) -> dict[str, object]:
    """The parsed file, which is read for WANTED, such as `[table] key`."""
    where = path()
    try:
        text = where.read_bytes().decode()
    except FileNotFoundError as exc:
        message = f"{where} doesn't exist; create it and set {wanted}"
        raise ConfigError(message) from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        message = f"cannot parse {where}: {exc}"
        raise ConfigError(message) from exc


def _value(table: str, key: str) -> object:
    where = path()
    section = _load(f"[{table}] {key}").get(table)
    value = section.get(key) if isinstance(section, dict) else None
    if value is None:
        message = f"{where} sets no [{table}] {key}"
        raise ConfigError(message)
    return value


def table(name: str) -> dict[str, object] | None:
    """The config's [NAME] table, or None when the file has none.

    Raises:
        ConfigError: The file is missing or isn't TOML, or NAME isn't a table.
    """
    section = _load(f"[{name}]").get(name)
    if section is None:
        return None
    if not isinstance(section, dict):
        message = f"{path()}: {name} must be a table"
        raise ConfigError(message)
    values: dict[str, object] = section
    return values


def string(table: str, key: str) -> str:
    """KEY in the config's [TABLE], a non-empty string.

    Raises:
        ConfigError: The file or KEY is missing, or KEY isn't such a string.
    """
    value = _value(table, key)
    if not isinstance(value, str) or not value:
        message = f"{path()}: [{table}] {key} must be a non-empty string"
        raise ConfigError(message)
    return value


def optional_table(name: str) -> dict[str, object] | None:
    """The config's [NAME] table, or None when the file or the table is missing.

    Raises:
        ConfigError: The file isn't TOML, or NAME isn't a table.
    """
    return table(name) if path().exists() else None


def optional_string(table: str, key: str) -> str | None:
    """KEY in the config's [TABLE], or None when the file, table or key is missing.

    Raises:
        ConfigError: KEY isn't a non-empty string.
    """
    return string(table, key) if _present(table, key) else None


def optional_strings(table: str, key: str) -> list[str] | None:
    """KEY in the config's [TABLE], or None when the file, table or key is missing.

    Raises:
        ConfigError: KEY isn't a non-empty list of non-empty strings.
    """
    return strings(table, key) if _present(table, key) else None


def _present(name: str, key: str) -> bool:
    section = optional_table(name)
    return section is not None and key in section


def strings(table: str, key: str) -> list[str]:
    """KEY in the config's [TABLE], a non-empty list of non-empty strings.

    Raises:
        ConfigError: The file or KEY is missing, or KEY isn't such a list.
    """
    value = _value(table, key)
    entries: list[object] = value if isinstance(value, list) else []
    if not entries or not all(isinstance(e, str) and e for e in entries):
        message = f"{path()}: [{table}] {key} must be a non-empty list of strings"
        raise ConfigError(message)
    return [str(e) for e in entries]


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_string(text: str) -> str:
    # JSON's escapes are TOML's, except that TOML also refuses a raw DEL. This is
    # what json.dumps(text, ensure_ascii=False) returns for a string.
    escaped = encode_basestring(text)
    return escaped.replace("\x7f", "\\u007f")


def _toml_key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else _toml_string(name)


def _toml_value(value: object) -> str | None:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        entries: list[object] = value
        if all(isinstance(e, str) for e in entries):
            return f"[{', '.join(_toml_string(str(e)) for e in entries)}]"
    return None


def render(table: str, key: str, value: list[str]) -> str:
    """The config file's text with [TABLE] KEY set to VALUE.

    VALUE replaces any KEY the file has, or else follows the table's other
    keys. The text holds every other value as the file does, but not its
    comments or layout.

    Raises:
        ConfigError: The file is missing or isn't TOML, or holds a value
            outside a table or other than a string or a list of strings.
    """
    data = _load(f"[{table}] {key}")
    data.setdefault(table, {})
    lines: list[str] = []
    for name, section in data.items():
        if not isinstance(section, dict):
            message = f"{path()}: {name} must be a table"
            raise ConfigError(message)
        values: dict[str, object] = section
        if name == table:
            values[key] = value
        lines += ["", f"[{_toml_key(name)}]"]
        for entry, item in values.items():
            text = _toml_value(item)
            if text is None:
                message = (
                    f"{path()}: [{name}] {entry} must be a string or a list of strings"
                )
                raise ConfigError(message)
            lines.append(f"{_toml_key(entry)} = {text}")
    return "\n".join(lines[1:]) + "\n"
