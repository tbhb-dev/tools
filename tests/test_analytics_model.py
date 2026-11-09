"""Analytics values cannot change their fields after construction."""

from dataclasses import FrozenInstanceError

import pytest

from tbhbdev.analytics.model import (
    Catalog,
    Migration,
    Relation,
    Role,
    Settings,
    Verdict,
)


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (Settings(), "host"),
        (Role("reader"), "name"),
        (Migration("0001_a.sql", 1), "name"),
        (Catalog(), "schemas"),
        (Verdict(), "violations"),
        (Relation("a", "v", ()), "name"),
    ],
)
def test_values_are_frozen(value: object, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(value, field, "replacement")
