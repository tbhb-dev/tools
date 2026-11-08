"""Required loop settings from the optional user config's [loop] table.

Loading settings reads one file and has no other effects. In particular, it
does not open the store, contact a host or execute the approval command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from pathlib import Path

from tbhbdev import userconfig

_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class LoopConfig:
    """Validated settings; collections are immutable and argv stays separate."""

    repositories: tuple[str, ...]
    approval_command: tuple[str, ...]
    store_path: Path
    reconcile_interval_minutes: int
    spawn_cap_total: int
    spawn_cap_per_host: int
    daily_spawn_cap: int
    hosts: tuple[str, ...]


def _error(key: str, why: str) -> userconfig.ConfigError:
    return userconfig.ConfigError(f"{userconfig.path()}: [loop] {key} {why}")


def _value(settings: dict[str, object], key: str) -> object:
    if key not in settings:
        raise _error(key, "is required")
    return settings[key]


def _strings(settings: dict[str, object], key: str) -> tuple[str, ...]:
    value = _value(settings, key)
    entries: list[object] = value if isinstance(value, list) else []
    if not entries or not all(isinstance(e, str) and e.strip() for e in entries):
        raise _error(key, "must be a non-empty list of non-blank strings")
    return tuple(str(e) for e in entries)


def _positive_int(settings: dict[str, object], key: str) -> int:
    value = _value(settings, key)
    if type(value) is not int or value <= 0:
        raise _error(key, "must be a positive integer")
    return value


def _unique_strings(settings: dict[str, object], key: str) -> tuple[str, ...]:
    entries = _strings(settings, key)
    if len(set(entries)) != len(entries):
        raise _error(key, "must not contain duplicates")
    return entries


def _store_path(settings: dict[str, object]) -> Path:
    value = _value(settings, "store_path")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        error = _error("store_path", "must be a non-blank path string without NUL")
        raise error
    store = Path(value).expanduser()
    if not store.is_absolute():
        error = _error("store_path", "must be an absolute path")
        raise error
    return store


def load() -> LoopConfig:
    """Read and validate every setting, with no deployment defaults.

    Raises:
        userconfig.ConfigError: The file, table or a setting is missing or invalid.
    """
    try:
        settings = userconfig.table("loop")
    except (userconfig.ConfigError, OSError, UnicodeError) as exc:
        error = userconfig.ConfigError(
            f"{userconfig.path()}: config cannot be read: {exc}"
        )
        raise error from exc
    if settings is None:
        error = _error("repositories", "is required")
        raise error
    unknown = settings.keys() - {field.name for field in fields(LoopConfig)}
    if unknown:
        raise _error(", ".join(sorted(unknown)), "not recognized")
    repositories = _unique_strings(settings, "repositories")
    if not all(_REPOSITORY.fullmatch(repo) for repo in repositories):
        error = _error("repositories", "must list owner/name strings")
        raise error
    loaded = LoopConfig(
        repositories=repositories,
        approval_command=_strings(settings, "approval_command"),
        store_path=_store_path(settings),
        reconcile_interval_minutes=_positive_int(
            settings, "reconcile_interval_minutes"
        ),
        spawn_cap_total=_positive_int(settings, "spawn_cap_total"),
        spawn_cap_per_host=_positive_int(settings, "spawn_cap_per_host"),
        daily_spawn_cap=_positive_int(settings, "daily_spawn_cap"),
        hosts=_unique_strings(settings, "hosts"),
    )
    if loaded.spawn_cap_per_host > loaded.spawn_cap_total:
        error = _error("spawn_cap_per_host", "must not exceed spawn_cap_total")
        raise error
    return loaded
