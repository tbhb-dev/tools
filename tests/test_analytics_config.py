"""Connection settings accept known keys and reject mistyped values."""

from dataclasses import asdict
from itertools import combinations

import pytest

from tbhbdev.analytics.decisions.config import parse
from tbhbdev.analytics.model import ConfigError, Settings

ROLE_KEYS = ("owner_role", "loader_role", "reader_role", "grafana_role")

TEXT_KEYS = (
    "host",
    "database",
    "user",
    "password_file",
    "reader_password_file",
    "grafana_password_file",
    "loader_password_file",
    "owner_role",
    "loader_role",
    "reader_role",
    "grafana_role",
)


def test_defaults() -> None:
    assert parse({}) == Settings(
        host="localhost",
        port=5432,
        database="analytics",
        user="analytics_owner",
        password_file=None,
        reader_password_file=None,
        grafana_password_file=None,
        loader_password_file=None,
        owner_role="analytics_owner",
        loader_role="analytics_loader",
        reader_role="analytics_reader",
        grafana_role="grafana_reader",
    )


@pytest.mark.parametrize("key", TEXT_KEYS)
def test_each_string_override(key: str) -> None:
    assert asdict(parse({key: "custom-value"}))[key] == "custom-value"


@pytest.mark.parametrize("key", TEXT_KEYS)
@pytest.mark.parametrize("value", [1, True, [], {}, "", " \t\n"])
def test_invalid_string_names_key(key: str, value: object) -> None:
    with pytest.raises(ValueError, match="analytics") as caught:
        parse({key: value})
    assert str(caught.value) == f"[analytics] {key} must be a non-blank string"


@pytest.mark.parametrize("key", TEXT_KEYS)
def test_none_is_only_valid_for_password_files(key: str) -> None:
    if key.endswith("password_file"):
        assert asdict(parse({key: None}))[key] is None
    else:
        with pytest.raises(ValueError, match=key):
            parse({key: None})


@pytest.mark.parametrize("port", [1, 5433, 65535])
def test_valid_ports(port: int) -> None:
    assert parse({"port": port}).port == port


@pytest.mark.parametrize("value", [None, True, False, "5432", 1.5, 0, -1, 65536])
def test_invalid_ports(value: object) -> None:
    with pytest.raises(ValueError, match="analytics") as caught:
        parse({"port": value})
    assert str(caught.value) == "[analytics] port must be an integer from 1 to 65535"


@pytest.mark.parametrize("key", ["unknown", "password", "roles"])
def test_unknown_keys(key: str) -> None:
    with pytest.raises(ValueError, match="analytics") as caught:
        parse({key: "example"})
    assert str(caught.value) == f"[analytics] {key} not recognized"


def test_parse_does_not_change_the_mapping() -> None:
    table: dict[str, object] = {"host": "example-db", "password_file": "password.txt"}
    original = dict(table)
    assert parse(table).password_file == table["password_file"]
    assert table == original


def test_default_role_names() -> None:
    settings = parse({})
    assert (
        settings.owner_role,
        settings.loader_role,
        settings.reader_role,
        settings.grafana_role,
    ) == ("analytics_owner", "analytics_loader", "analytics_reader", "grafana_reader")


@pytest.mark.parametrize("enabled", [True, False])
def test_loader_switch(enabled: bool) -> None:
    assert parse({"loader_enabled": enabled}).loader_enabled is enabled


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_invalid_loader_switch(value: object) -> None:
    with pytest.raises(ValueError, match=r".") as caught:
        parse({"loader_enabled": value})
    assert str(caught.value) == "[analytics] loader_enabled must be a boolean"


@pytest.mark.parametrize(("earlier", "later"), list(combinations(ROLE_KEYS, 2)))
def test_equal_role_names_are_refused(earlier: str, later: str) -> None:
    with pytest.raises(ConfigError) as caught:
        parse({earlier: "shared", later: "shared"})
    assert str(caught.value) == f"[analytics] {later} and {earlier} must differ"


@pytest.mark.parametrize(("earlier", "later"), list(combinations(ROLE_KEYS, 2)))
def test_a_role_equal_to_another_default_is_refused(earlier: str, later: str) -> None:
    defaults = asdict(Settings())
    with pytest.raises(ConfigError, match="must differ"):
        parse({later: defaults[earlier]})


def test_distinct_custom_role_names_pass() -> None:
    assert parse({key: f"custom_{key}" for key in ROLE_KEYS}).reader_role == (
        "custom_reader_role"
    )


@pytest.mark.parametrize(
    "table",
    [
        {"unknown": 1},
        {"loader_enabled": "x"},
        {"host": ""},
        {"port": 0},
        {"owner_role": "same", "reader_role": "same"},
    ],
)
def test_config_mistakes_are_config_errors(table: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse(table)
