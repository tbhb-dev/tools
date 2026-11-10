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
        "migration diagnostic: syntax error [redacted output] [credential]\n"
        "analytics: command failed\n"
    )
