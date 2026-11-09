"""Exact contracts and compatible additive catalog changes."""

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tbhbdev.analytics.decisions.contract import matches, preserves
from tbhbdev.analytics.model import Catalog, Relation, Verdict

OLD = Catalog(
    ("mart", "meta"),
    (Relation("mart.items", "v", (("id", "integer"), ("name", "text"))),),
    (("meta.as_of", "", "timestamp with time zone"),),
)


@pytest.mark.parametrize(
    ("candidate", "violations"),
    [
        (replace(OLD, schemas=("mart",)), ("missing schema: meta",)),
        (replace(OLD, relations=()), ("missing relation: mart.items",)),
        (
            replace(
                OLD, relations=(Relation("mart.other", "v", (("id", "integer"),)),)
            ),
            ("missing relation: mart.items",),
        ),
        (
            replace(OLD, relations=(replace(OLD.relations[0], kind="m"),)),
            ("relation kind changed: mart.items",),
        ),
        (
            replace(
                OLD,
                relations=(replace(OLD.relations[0], columns=(("id", "integer"),)),),
            ),
            ("missing column: mart.items.name",),
        ),
        (
            replace(
                OLD,
                relations=(
                    replace(
                        OLD.relations[0], columns=(("id", "bigint"), ("name", "text"))
                    ),
                ),
            ),
            ("column type changed: mart.items.id",),
        ),
        (
            replace(OLD, functions=()),
            ("missing function: meta.as_of() -> timestamp with time zone",),
        ),
        (
            replace(
                OLD, functions=(("meta.as_of", "integer", "timestamp with time zone"),)
            ),
            ("missing function: meta.as_of() -> timestamp with time zone",),
        ),
        (
            replace(OLD, functions=(("meta.as_of", "", "text"),)),
            ("missing function: meta.as_of() -> timestamp with time zone",),
        ),
    ],
)
def test_each_violation_names_the_object(
    candidate: Catalog, violations: tuple[str, ...]
) -> None:
    assert preserves(OLD, candidate) == Verdict(violations=violations)


def test_all_violations_reach_the_result() -> None:
    assert preserves(OLD, Catalog()) == Verdict(
        violations=(
            "missing schema: mart",
            "missing schema: meta",
            "missing relation: mart.items",
            "missing function: meta.as_of() -> timestamp with time zone",
        )
    )


def test_additions_are_compatible_but_change_the_contract() -> None:
    new = Catalog(
        (*OLD.schemas, "raw"),
        (
            replace(
                OLD.relations[0], columns=(*OLD.relations[0].columns, ("extra", "text"))
            ),
            Relation("raw.events", "r", ()),
        ),
        (*OLD.functions, ("meta.other", "", "text")),
    )
    assert preserves(OLD, new) == Verdict()
    assert matches(new, OLD) == Verdict(
        violations=(
            "missing schema: raw",
            "missing column: mart.items.extra",
            "missing relation: raw.events",
            "missing function: meta.other() -> text",
            "catalog differs from contract",
        )
    )


def test_exact_comparison_retains_array_order() -> None:
    assert matches(replace(OLD, schemas=tuple(reversed(OLD.schemas))), OLD) == Verdict(
        violations=("catalog differs from contract",)
    )


def test_exact_comparison_reports_both_directions() -> None:
    changed = replace(OLD, schemas=("raw",))
    assert matches(changed, OLD) == Verdict(
        violations=(
            "missing schema: mart",
            "missing schema: meta",
            "missing schema: raw",
            "catalog differs from contract",
        )
    )


@given(
    st.lists(st.text(), unique=True),
    st.lists(st.tuples(st.text(), st.text()), unique_by=lambda column: column[0]),
    st.lists(st.tuples(st.text(), st.text(), st.text())),
)
def test_comparisons_are_reflexive(
    schemas: list[str],
    columns: list[tuple[str, str]],
    functions: list[tuple[str, str, str]],
) -> None:
    catalog = Catalog(
        tuple(schemas), (Relation("items", "r", tuple(columns)),), tuple(functions)
    )
    assert preserves(catalog, catalog) == Verdict()
    assert matches(catalog, catalog) == Verdict()


def test_missing_relation_does_not_hide_later_violations() -> None:
    old = Catalog(
        relations=(
            Relation("mart.first", "v", ()),
            Relation("mart.second", "r", (("id", "integer"), ("label", "text"))),
        )
    )
    new = Catalog(relations=(Relation("mart.second", "v", (("id", "bigint"),)),))
    assert preserves(old, new) == Verdict(
        violations=(
            "missing relation: mart.first",
            "relation kind changed: mart.second",
            "column type changed: mart.second.id",
            "missing column: mart.second.label",
        )
    )
