"""Version parsing and pending migration selection."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tbhbdev.analytics.decisions import migrations
from tbhbdev.analytics.model import Migration


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("0001_initial.sql", 1),
        ("10_more.sql", 10),
        ("00002_.sql", 2),
        ("12345_next.sql", 12345),
    ],
)
def test_positive_versions(name: str, version: int) -> None:
    assert migrations.parse(name) == Migration(name, version)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "initial.sql",
        "0000_initial.sql",
        "0_zero.sql",
        "-1_bad.sql",
        "1.sql",
        "1x_bad.sql",
        "1_name.txt",
        "1_name.sql.bak",
        "a/0001_initial.sql",
        "\u0661_name.sql",
    ],
)
def test_rejects_names_without_positive_versions(name: str) -> None:
    with pytest.raises(
        ValueError, match=r"^migration filename has no positive version$"
    ):
        migrations.parse(name)


@given(
    st.integers(min_value=1, max_value=999999), st.integers(min_value=0, max_value=8)
)
def test_leading_zeroes_do_not_change_version(version: int, zeroes: int) -> None:
    name = f"{'0' * zeroes}{version}_migration.sql"
    assert migrations.parse(name) == Migration(name, version)


@pytest.mark.parametrize(
    ("applied", "expected"),
    [((), (1, 2, 10)), ((1, 999), (2, 10)), ((1, 2, 10), ()), ((999,), (1, 2, 10))],
)
def test_pending_keeps_filename_order_and_tolerates_newer_recorded_versions(
    applied: tuple[int, ...], expected: tuple[int, ...]
) -> None:
    names = ["0010_last.sql", "0002_second.sql", "0001_first.sql"]
    assert migrations.pending(iter(names), iter(applied)) == tuple(
        Migration(name, version)
        for name, version in zip(sorted(names), (1, 2, 10), strict=True)
        if version in expected
    )


def test_pending_validates_even_an_applied_name() -> None:
    with pytest.raises(ValueError, match="positive version"):
        migrations.pending(["0000_bad.sql"], [0])


def test_pending_orders_by_filename_instead_of_numeric_version() -> None:
    assert migrations.pending(["2_b.sql", "10_a.sql"], []) == (
        Migration("10_a.sql", 10),
        Migration("2_b.sql", 2),
    )


def test_duplicate_versions_apply_only_the_first_file() -> None:
    assert migrations.pending(["0001_b.sql", "0001_a.sql", "0002_c.sql"], []) == (
        Migration("0001_a.sql", 1),
        Migration("0002_c.sql", 2),
    )
