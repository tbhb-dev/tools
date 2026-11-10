"""Catalog queries preserve ordering and every contract field."""

import json
from copy import deepcopy
from importlib.resources import files
from typing import cast

import pytest
from analytics_fakes import FakeConnection, packaged

from tbhbdev.analytics import catalog
from tbhbdev.analytics.model import Catalog, Relation

DATA: catalog.CatalogData = {
    "schemas": ["z", "a"],
    "relations": [
        {
            "name": "z.items",
            "kind": "r",
            "columns": [{"name": "z", "type": "bigint"}, {"name": "a", "type": "text"}],
        }
    ],
    "functions": [{"name": "z.fn", "arguments": "x integer", "returns": "text"}],
}
EXPECTED = Catalog(
    ("z", "a"),
    (Relation("z.items", "r", (("z", "bigint"), ("a", "text"))),),
    (("z.fn", "x integer", "text"),),
)


@pytest.mark.parametrize("encoded", [False, True])
def test_read(encoded: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        catalog,
        "files",
        packaged(
            files("tbhbdev.analytics").joinpath("data", "catalog.sql"),
            "data",
            "catalog.sql",
        ),
    )
    fake = FakeConnection()
    fake.catalog = json.dumps(DATA) if encoded else DATA
    assert catalog.read(fake) == EXPECTED
    assert fake.calls == [
        (files("tbhbdev.analytics").joinpath("data", "catalog.sql").read_text(), None)
    ]


def test_packaged_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        catalog,
        "files",
        packaged(
            files("tbhbdev.analytics").joinpath("data", "contract.json"),
            "data",
            "contract.json",
        ),
    )
    expected = catalog.parse(
        json.loads(
            files("tbhbdev.analytics").joinpath("data", "contract.json").read_text()
        )
    )
    assert catalog.contract() == expected
    assert len(expected.schemas) == 6
    assert len(expected.relations) > 1
    assert expected.functions


@pytest.mark.parametrize("level", ["catalog", "relation", "column", "function"])
@pytest.mark.parametrize("extra", [False, True])
def test_exact_fields(level: str, extra: bool) -> None:
    data = deepcopy(DATA)
    records = {
        "catalog": data,
        "relation": data["relations"][0],
        "column": data["relations"][0]["columns"][0],
        "function": data["functions"][0],
    }
    record = cast("dict[str, object]", records[level])
    if extra:
        record["extra"] = "unexpected"
    else:
        del record[next(iter(record))]
    with pytest.raises(
        ValueError, match=r"^catalog fields differ from contract format$"
    ):
        catalog.parse(data)
