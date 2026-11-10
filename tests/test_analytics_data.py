"""Installed analytics resources retain the exact exported bytes."""

import hashlib
import json
from importlib.resources import files

import pytest


@pytest.mark.parametrize(
    ("name", "digest"),
    [
        (
            "catalog.sql",
            "7955982b9604341eb4bb398d19ab336d99aae7926881aae698b9e1861ef462be",
        ),
        (
            "contract.json",
            "72be374ff73d2ebfdba77143ca18d2b2ce298ff9695fc1ccc2246673856ca170",
        ),
        (
            "fixtures/smoke.sql",
            "796e6ebf8e6b87410623a7bf03f52a0823501ec126a4ce86a897205e3da9ada6",
        ),
        (
            "migrations/0001_initial.sql",
            "ddd07aac78ea5757bfa8a8911a114088bf1377de759ab5d18d1b89760ac4abff",
        ),
    ],
)
def test_packaged_data(name: str, digest: str) -> None:
    data = files("tbhbdev.analytics").joinpath("data", name).read_bytes()
    assert data
    assert hashlib.sha256(data).hexdigest() == digest


def test_contract_is_json() -> None:
    contract = json.loads(
        files("tbhbdev.analytics").joinpath("data", "contract.json").read_text()
    )
    assert set(contract) == {"schemas", "relations", "functions"}
    assert all(contract.values())
