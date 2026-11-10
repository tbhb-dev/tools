"""Owner connections, lock scope and independently committed migrations."""

import sys
from dataclasses import replace
from importlib.resources import files
from types import SimpleNamespace
lazy from pathlib import Path

import pytest
from analytics_fakes import FakeConnection, answer, packaged
from hypothesis import given
from hypothesis import strategies as st

from tbhbdev.analytics import database
from tbhbdev.analytics.decisions import roles, scram
from tbhbdev.analytics.model import Migration, Settings


@pytest.fixture
def options(tmp_path: Path) -> Settings:
    for key in ("owner", "reader", "grafana_reader", "loader"):
        (tmp_path / f"{key}_password").write_text(key[0] * 32 + "\n")
    return Settings(
        password_file=str(tmp_path / "owner_password"),
        reader_password_file=str(tmp_path / "reader_password"),
        grafana_password_file=str(tmp_path / "grafana_reader_password"),
        loader_password_file=str(tmp_path / "loader_password"),
    )


@given(
    value=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        min_size=32,
        max_size=32,
    )
)
def test_password_first_line(
    value: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("password") / "owner_password"
    path.write_text(value + "\nignored\nextra")
    assert database.password(path) == value


@pytest.mark.parametrize(
    "value", ["", "a" * 31, "a" * 33, "a" * 31 + "!", "é" * 32, "a" * 32 + "\r\n"]
)
def test_damaged_password(tmp_path: Path, value: str) -> None:
    path = tmp_path / "password"
    path.write_text(value)
    with pytest.raises(ValueError, match=r"^password file damaged$"):
        database.password(path)


def test_driver_boundary(monkeypatch: pytest.MonkeyPatch, options: Settings) -> None:
    fake = FakeConnection()
    calls: list[dict[str, object]] = []

    def connect(**kwargs: object) -> FakeConnection:
        calls.append(kwargs)
        return fake

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    options = replace(
        options,
        host="database",
        port=5433,
        database="warehouse",
        user="owner",
        owner_role="owner",
    )
    assert database.connect(options) is fake
    assert calls == [
        {
            "host": "database",
            "port": 5433,
            "dbname": "warehouse",
            "user": "owner",
            "password": "o" * 32,
            "connect_timeout": 10,
            "autocommit": True,
        }
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (
            Settings(user="postgres"),
            "analytics requires the owner role as the connection user",
        ),
        (Settings(), "[analytics] password_file is required"),
    ],
)
def test_connect_refuses(options: Settings, message: str) -> None:
    with pytest.raises(ValueError, match=r".") as caught:
        database.connect(options)
    assert str(caught.value) == message


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("existing_roles", [False, True])
def test_roles_and_repeat(
    options: Settings,
    enabled: bool,
    existing_roles: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        database,
        "files",
        packaged(
            files("tbhbdev.analytics").joinpath("data", "migrations"),
            "data",
            "migrations",
        ),
    )
    options = replace(options, loader_enabled=enabled)
    fake = FakeConnection()
    if existing_roles:
        fake.roles = {role.name for role, _ in roles.selected(options)}
    assert database.migrate(fake, options) == (Migration("0001_initial.sql", 1),)
    assert fake.events == ["begin", "commit"]
    assert fake.versions == [1]
    statements = [sql for sql, _ in fake.calls]
    assert statements[0] == "SELECT pg_advisory_lock(62001)"
    assert statements[-1] == "SELECT pg_advisory_unlock(62001)"
    assert statements[1:3] == [
        "CREATE SCHEMA IF NOT EXISTS meta",
        (
            "CREATE TABLE IF NOT EXISTS meta.migration "
            "(version integer PRIMARY KEY, applied_at timestamptz)"
        ),
    ]
    for role, key in roles.selected(options):
        quoted = roles.identifier(role.name)
        assert (f"CREATE ROLE {quoted} LOGIN" in statements) is not existing_roles
        hashed = scram.verifier(key[0] * 32, scram.stable_salt("o" * 32, key))
        assert f"ALTER ROLE {quoted} PASSWORD '{hashed}'" in statements
    assert ('ALTER ROLE "grafana_reader" CONNECTION LIMIT 5') in statements
    assert ("ALTER ROLE \"grafana_reader\" SET statement_timeout = '10s'") in statements
    assert tuple(
        statements[-len(roles.statements(options)) - 1 : -1]
    ) == roles.statements(options)
    assert ("INSERT INTO meta.migration VALUES (%s, now())", (1,)) in fake.calls
    fake.versions.append(99)
    assert database.migrate(fake, options) == ()
    assert fake.versions == [1, 99]
    assert fake.events == ["begin", "commit"]


@pytest.fixture
def packaged_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        database,
        "files",
        packaged(
            files("tbhbdev.analytics").joinpath("data", "migrations"),
            "data",
            "migrations",
        ),
    )


ROLE_FILES = {
    "loader": "loader_password_file",
    "reader": "reader_password_file",
    "grafana_reader": "grafana_password_file",
}


@pytest.mark.usefixtures("packaged_migrations")
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("unconfigured", ["loader", "reader", "grafana_reader"])
def test_role_without_password_file(
    options: Settings, unconfigured: str, existing: bool
) -> None:
    options = replace(options, loader_enabled=True, **{ROLE_FILES[unconfigured]: None})
    fake = FakeConnection()
    if existing:
        fake.roles = {role.name for role, _ in roles.selected(options)}
    database.migrate(fake, options)
    statements = [sql for sql, _ in fake.calls]
    for role, key in roles.selected(options):
        quoted = roles.identifier(role.name)
        created = f"CREATE ROLE {quoted} LOGIN" in statements
        assert created is not existing
        hashed = scram.verifier(key[0] * 32, scram.stable_salt("o" * 32, key))
        altered = f"ALTER ROLE {quoted} PASSWORD '{hashed}'" in statements
        assert altered is (key != unconfigured)
        assert not any(
            sql.startswith(f"ALTER ROLE {quoted} PASSWORD")
            for sql in statements
            if key == unconfigured
        )
    assert roles.passwordless(options) == (
        (
            {
                "loader": "analytics_loader",
                "reader": "analytics_reader",
                "grafana_reader": "grafana_reader",
            }[unconfigured],
            ROLE_FILES[unconfigured],
        ),
    )


@pytest.mark.usefixtures("packaged_migrations")
def test_unconfigured_files_need_no_other_files(tmp_path: Path) -> None:
    (tmp_path / "owner_password").write_text("o" * 32)
    fake = FakeConnection()
    options = Settings(password_file=str(tmp_path / "owner_password"))
    database.migrate(fake, options)
    statements = [sql for sql, _ in fake.calls]
    assert not any("PASSWORD" in sql for sql in statements)
    assert 'CREATE ROLE "analytics_reader" LOGIN' in statements
    assert 'CREATE ROLE "grafana_reader" LOGIN' in statements
    assert 'CREATE ROLE "analytics_loader" LOGIN' not in statements


@pytest.mark.parametrize("exists", [False, True])
def test_dry_run(options: Settings, exists: bool) -> None:
    fake = FakeConnection()
    fake.exists = exists
    assert database.migrate(fake, options, dry_run=True) == (
        Migration("0001_initial.sql", 1),
    )
    assert fake.events == []
    assert fake.versions == []
    assert fake.calls == [
        ("SELECT pg_advisory_lock(62001)", None),
        ("SELECT to_regclass('meta.migration')", None),
        *(([("SELECT version FROM meta.migration", None)]) if exists else []),
        ("SELECT pg_advisory_unlock(62001)", None),
    ]


def test_transaction_rollback_and_unlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: Settings
) -> None:
    directory = tmp_path / "data" / "migrations"
    directory.mkdir(parents=True)
    (directory / "0001_ok.sql").write_text("first")
    (directory / "0002_bad.sql").write_text("bad")
    (directory / "ignored").mkdir()
    monkeypatch.setattr(database, "files", answer(tmp_path))
    fake = FakeConnection()
    fake.failure = "bad"
    with pytest.raises(ValueError, match="syntax error"):
        database.migrate(fake, options)
    assert fake.events == ["begin", "commit", "begin", "rollback"]
    assert fake.versions == [1]
    assert fake.calls[-1] == ("SELECT pg_advisory_unlock(62001)", None)
    assert not any(sql in roles.statements(options) for sql, _ in fake.calls)
    fake.failure = ""
    assert database.migrate(fake, options) == (Migration("0002_bad.sql", 2),)
    assert fake.versions == [1, 2]


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        ".DS_Store",
        "notes.sql",
        "a0001_x.sql",
        "0001_x.txt",
        "\u0661_x.sql",
    ],
)
def test_stray_names_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: Settings, name: str
) -> None:
    directory = tmp_path / "data" / "migrations"
    directory.mkdir(parents=True)
    (directory / "0001_ok.sql").write_text("first")
    (directory / name).write_text("stray")
    monkeypatch.setattr(database, "files", answer(tmp_path))
    fake = FakeConnection()
    assert database.migrate(fake, options, dry_run=True) == (
        Migration("0001_ok.sql", 1),
    )


def test_matching_name_without_positive_version_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: Settings
) -> None:
    directory = tmp_path / "data" / "migrations"
    directory.mkdir(parents=True)
    (directory / "0000_x.sql").write_text("zero")
    monkeypatch.setattr(database, "files", answer(tmp_path))
    with pytest.raises(ValueError, match="positive version"):
        database.migrate(FakeConnection(), options, dry_run=True)


@pytest.mark.parametrize(
    "failure", ["pg_advisory_lock", "CREATE SCHEMA", "PASSWORD", "REVOKE CONNECT"]
)
def test_failures_propagate_and_release(options: Settings, failure: str) -> None:
    fake = FakeConnection()
    fake.failure = failure
    with pytest.raises(ValueError, match=r"."):
        database.migrate(fake, options)
    assert fake.calls[-1][0] == (
        "SELECT pg_advisory_lock(62001)"
        if failure == "pg_advisory_lock"
        else "SELECT pg_advisory_unlock(62001)"
    )
