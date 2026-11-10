"""Migration versions and the pending files in filename order."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

from tbhbdev.analytics.model import Migration


def parse(name: str) -> Migration:
    match = re.fullmatch(r"([0-9]+)_[^/]*\.sql", name)
    if match is None or int(match[1]) == 0:
        message = "migration filename has no positive version"
        raise ValueError(message)
    return Migration(name, int(match[1]))


def sql_names(names: Iterable[str]) -> Iterator[str]:
    """Keep names the old `[0-9]*.sql` glob matched; others are not migrations."""
    return (name for name in names if "0" <= name[:1] <= "9" and name.endswith(".sql"))


def pending(names: Iterable[str], applied: Iterable[int]) -> tuple[Migration, ...]:
    recorded = set(applied)
    selected: list[Migration] = []
    for name in sorted(names):
        migration = parse(name)
        if migration.version not in recorded:
            selected.append(migration)
            recorded.add(migration.version)
    return tuple(selected)
