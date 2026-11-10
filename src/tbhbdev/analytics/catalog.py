"""Read the observed catalog and the packaged contract without normalization."""

import json
from importlib.resources import files
from typing import TYPE_CHECKING, TypedDict, cast

from tbhbdev.analytics.model import Catalog, Relation

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tbhbdev.analytics.database import Connection


class ColumnData(TypedDict):
    name: str
    type: str


class RelationData(TypedDict):
    name: str
    kind: str
    columns: list[ColumnData]


class FunctionData(TypedDict):
    name: str
    arguments: str
    returns: str


class CatalogData(TypedDict):
    schemas: list[str]
    relations: list[RelationData]
    functions: list[FunctionData]


def parse(value: object) -> Catalog:
    # The type argument has no runtime effect.
    data = cast(
        "CatalogData",  # pragma: no mutate
        value,
    )
    _fields(data, ("schemas", "relations", "functions"))
    for entry in data["relations"]:
        _fields(entry, ("name", "kind", "columns"))
        for column in entry["columns"]:
            _fields(column, ("name", "type"))
    for function in data["functions"]:
        _fields(function, ("name", "arguments", "returns"))
    return Catalog(
        schemas=tuple(data["schemas"]),
        relations=tuple(
            Relation(
                entry["name"],
                entry["kind"],
                tuple((column["name"], column["type"]) for column in entry["columns"]),
            )
            for entry in data["relations"]
        ),
        functions=tuple(
            (entry["name"], entry["arguments"], entry["returns"])
            for entry in data["functions"]
        ),
    )


def read(connection: Connection) -> Catalog:
    statement = files("tbhbdev.analytics").joinpath("data", "catalog.sql").read_text()
    value = connection.execute(statement).fetchall()[0][0]
    return parse(json.loads(value) if isinstance(value, str) else value)


def contract() -> Catalog:
    return parse(
        json.loads(
            files("tbhbdev.analytics").joinpath("data", "contract.json").read_text()
        )
    )


def _fields(record: Mapping[str, object], expected: tuple[str, ...]) -> None:
    if set(record) != set(expected):
        message = "catalog fields differ from contract format"
        raise ValueError(message)
