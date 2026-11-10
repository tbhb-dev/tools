"""Configuration reads happen once and environment values override the table."""

from dataclasses import asdict
lazy from pathlib import Path

import pytest
from analytics_fakes import answer

from tbhbdev import userconfig
from tbhbdev.analytics import settings
from tbhbdev.analytics.model import ConfigError, Settings


@pytest.mark.parametrize("present", [False, True])
def test_read_once(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    calls: list[str] = []

    def table(key: str) -> dict[str, object] | None:
        calls.append(key)
        return {"loader_enabled": True} if present else None

    for variable in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(userconfig, "optional_table", table)
    assert settings.load() == Settings(loader_enabled=present)
    assert calls == ["analytics"]


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = tmp_path / "tbhbdev"
    folder.mkdir()
    (folder / "config.toml").write_text(
        '[analytics]\nhost="config-db"\nport=5433\ndatabase="old"\n'
        'user="old"\npassword_file="owner_password"\nloader_enabled=true\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for variable, value in (
        ("PGHOST", "environment-db"),
        ("PGPORT", "5434"),
        ("PGDATABASE", "warehouse"),
        ("PGUSER", "analytics_owner"),
    ):
        monkeypatch.setenv(variable, value)
    assert asdict(settings.load()) == asdict(
        Settings(
            host="environment-db",
            port=5434,
            database="warehouse",
            password_file="owner_password",  # noqa: S106 - fixture path
            loader_enabled=True,
        )
    )


@pytest.mark.parametrize(
    ("user", "message"),
    [
        ("postgres", "analytics requires the owner role as the connection user"),
        ("reader", "analytics requires the owner role as the connection user"),
        ("", "[analytics] user must be a non-blank string"),
    ],
)
def test_refuse_other_user(
    monkeypatch: pytest.MonkeyPatch, user: str, message: str
) -> None:
    monkeypatch.setattr(userconfig, "optional_table", answer({}))
    monkeypatch.setenv("PGUSER", user)
    with pytest.raises(ConfigError, match=r".") as caught:
        settings.load()
    assert str(caught.value) == message


@pytest.mark.parametrize("port", ["0", "65536"])
def test_out_of_range_environment_port(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setattr(userconfig, "optional_table", answer({}))
    monkeypatch.setenv("PGPORT", port)
    with pytest.raises(ConfigError, match="port must be an integer"):
        settings.load()


def test_unparsable_environment_port_stays_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(userconfig, "optional_table", answer({}))
    monkeypatch.setenv("PGPORT", "invalid")
    # int() puts the variable's value in its message, so this isn't a ConfigError.
    with pytest.raises(ValueError, match=r".") as caught:
        settings.load()
    assert not isinstance(caught.value, ConfigError)
