"""Codex's trust for hooks: the `hooks.state` entries `config.toml` holds.

Codex runs a hook from `hooks.json` only when `config.toml` holds a trusted
hash for it:

    [hooks.state."<hooks.json path>:<event>:<group>:<handler>"]
    trusted_hash = "sha256:<hex>"

The event is snake_case (`pre_tool_use`), and the group and handler are
0-based positions in `hooks.json`. The hash is SHA-256 of the compact JSON,
keys sorted, of the event, the group's matcher and the one handler, so a
hook that changes loses its trust.

This module computes those keys and hashes for the handlers a predicate
recognizes as ours, and edits the TOML text so that only those entries
change. Every other table, key, comment and entry stays as it was. A hook
that is not ours is never trusted, changed or removed.

An entry goes only when its key names this `hooks.json` and its hash is one
of ours, held by a key that no handler of ours now has. That entry is our own
hook at an old position. An entry for a position that now holds someone
else's hook has a hash that isn't ours, so it stays and Codex reports the
mismatch.
"""

import hashlib
import json
import re
from collections.abc import MutableMapping
from typing import TYPE_CHECKING

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

type Json = dict[str, object]

CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")
HASH_KEY = "trusted_hash"
# A key ends in three colon-separated parts: event, group and handler.
KEY_COLONS = 3


def event_label(event: str) -> str:
    """Codex's snake_case name for an event: `PreToolUse` is `pre_tool_use`."""
    return CAMEL_BOUNDARY.sub("_", event).lower()


def hook_key(path: Path, label: str, group: int, handler: int) -> str:
    """The `hooks.state` key of one handler."""
    return f"{path}:{label}:{group}:{handler}"


def hook_hash(label: str, matcher: object, command: object, timeout: object) -> str:
    """Codex's trusted hash of one command handler, as `sha256:<hex>`.

    MATCHER, COMMAND and TIMEOUT are as `hooks.json` holds them. A group
    without a matcher (None) hashes without that key.
    """
    hashed: Json = {
        "event_name": label,
        "hooks": [
            {"type": "command", "command": command, "timeout": timeout, "async": False}
        ],
    }
    if matcher is not None:
        hashed["matcher"] = matcher
    compact = json.dumps(hashed, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(compact.encode()).hexdigest()}"


def as_list(value: object) -> list[object] | None:
    """VALUE as a list of objects, None when it is not a list."""
    return list[object](value) if isinstance(value, list) else None


def as_dict(value: object) -> Json | None:
    """VALUE as an object, None when it is not one."""
    return dict[str, object](value) if isinstance(value, dict) else None


def groups_of(config: Mapping[str, object]) -> Iterator[tuple[str, int, Json]]:
    """Each group of the `hooks.json` CONFIG: its event label, position and object.

    Positions count every group of an event. A group that isn't an object has
    a position but isn't listed.
    """
    events = as_dict(config.get("hooks")) or {}
    for event, value in events.items():
        label = event_label(event)
        for at, item in enumerate(as_list(value) or []):
            entry = as_dict(item)
            if entry is not None:
                yield label, at, entry


def our_hashes(
    path: Path, config: Mapping[str, object], is_ours: Callable[[object], bool]
) -> dict[str, str]:
    """Each of our handlers' `hooks.state` key, with its hash.

    CONFIG is the `hooks.json` at PATH as written. Positions count every group
    and handler in it, ours or not.
    """
    found: dict[str, str] = {}
    for label, at, group in groups_of(config):
        matcher = group.get("matcher")
        for number, item in enumerate(as_list(group.get("hooks")) or []):
            one = as_dict(item)
            if one is not None and is_ours(one):
                found[hook_key(path, label, at, number)] = hook_hash(
                    label, matcher, one["command"], one["timeout"]
                )
    return found


def sub_table(parent: Table | tomlkit.TOMLDocument, name: str, label: str) -> Table:
    """The table `name` in PARENT, made when missing.

    LABEL is the dotted name for the error when something else is there.
    """
    child = parent.get(name)
    if child is None:
        child = tomlkit.table()
        parent[name] = child
    elif not isinstance(child, Table):
        message = f"its {label} is not a table"
        raise ValueError(message)
    return child


def holds_old_position(key: str, path: Path) -> bool:
    """Whether KEY is a `hooks.state` key for a handler of the `hooks.json` at PATH."""
    parts = key.rsplit(":", KEY_COLONS)
    return len(parts) == KEY_COLONS + 1 and parts[0] == str(path)


def trusted_hash(entry: object) -> object:
    """The hash an entry holds, None when it holds none."""
    return entry.get(HASH_KEY) if isinstance(entry, MutableMapping) else None


def trusted_text(old: str | None, path: Path, wanted: Mapping[str, str]) -> str:
    """The `config.toml` text with WANTED trusted and our old entries gone.

    OLD is None for a missing file. WANTED maps each key to its hash, as
    `our_hashes` returns it, for the `hooks.json` at PATH. A ValueError names
    what is wrong with a file this can't edit.
    """
    try:
        doc = tomlkit.parse(old or "")
    except TOMLKitError as exc:
        message = f"it is not valid TOML ({exc})"
        raise ValueError(message) from exc
    state = sub_table(sub_table(doc, "hooks", "hooks"), "state", "hooks.state")
    ours = set(wanted.values())
    stale = [
        key
        for key in list[str](state)
        if key not in wanted
        and holds_old_position(key, path)
        and trusted_hash(state[key]) in ours
    ]
    for key in stale:
        del state[key]
    for key, digest in wanted.items():
        entry = state.get(key)
        if not isinstance(entry, MutableMapping):
            state[key] = tomlkit.item({HASH_KEY: digest})
        elif trusted_hash(entry) != digest:
            entry[HASH_KEY] = digest
    return tomlkit.dumps(doc)
