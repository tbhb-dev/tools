"""Command text has stable formats and never reproduces exception content."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tbhbdev.analytics import render
from tbhbdev.analytics.model import Migration, Verdict


@pytest.mark.parametrize(("dry_run", "word"), [(False, "applied"), (True, "pending")])
def test_migrated(dry_run: bool, word: str) -> None:
    assert render.migrated((), dry_run=dry_run) == f"analytics: 0 {word} migrations\n"
    assert render.migrated(
        (Migration("0001_a.sql", 1), Migration("0002_b.sql", 2)), dry_run=dry_run
    ) == (f"analytics: 2 {word} migrations\n1: 0001_a.sql\n2: 0002_b.sql\n")


@pytest.mark.parametrize("dry_run", [False, True])
def test_migrated_lists_roles_without_passwords(dry_run: bool) -> None:
    assert render.migrated(
        (Migration("0001_a.sql", 1),),
        dry_run=dry_run,
        passwordless=(
            ("analytics_reader", "reader_password_file"),
            ("grafana_reader", "grafana_password_file"),
        ),
    ) == (
        f"analytics: 1 {'pending' if dry_run else 'applied'} migrations\n"
        "1: 0001_a.sql\n"
        "role analytics_reader has no password "
        "(reader_password_file is not configured)\n"
        "role grafana_reader has no password "
        "(grafana_password_file is not configured)\n"
    )


def test_checked() -> None:
    assert render.checked(Verdict()) == "analytics: catalog matches contract\n"
    assert (
        render.checked(Verdict(("first", "second")))
        == "analytics: first\nanalytics: second\n"
    )


@given(st.text(alphabet=st.characters(categories=("L", "N")), min_size=40))
def test_failed_redacts(value: str) -> None:
    assert value not in render.failed(ValueError(value))


def test_failed_diagnostics() -> None:
    assert render.failed(ValueError("syntax error password sentinel")) == (
        "diagnostic: syntax error [redacted output] [credential]\n"
        "analytics: command failed\n"
    )


def test_failed_label_fits_every_command() -> None:
    assert "migration" not in render.failed(ValueError("error"))
