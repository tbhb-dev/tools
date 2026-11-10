"""Opt-in assertions against a caller-provided disposable PostgreSQL database.

Set three variables to run it. ``ANALYTICS_TEST_HOST`` selects the server,
``ANALYTICS_TEST_DATABASE`` names the one database the test connects to and
changes, and ``ANALYTICS_TEST_PASSWORD_FILE`` is the owner's password file. The
test ignores the user's ``[analytics]`` host, database and password file, since
the suite points ``XDG_CONFIG_HOME`` at an empty directory. It also ignores
``PGHOST``, ``PGPORT``, ``PGDATABASE`` and ``PGUSER`` from the shell, so no
other database can receive the migrations by accident.
"""

import os
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from analytics_fakes import answer

from tbhbdev.analytics import database, settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.analytics_server
def test_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host = os.environ.get("ANALYTICS_TEST_HOST")
    if not host:
        pytest.skip("ANALYTICS_TEST_HOST must select a disposable server")
    name = os.environ.get("ANALYTICS_TEST_DATABASE")
    if not name:
        pytest.skip("ANALYTICS_TEST_DATABASE must name a disposable database")
    password_file = os.environ.get("ANALYTICS_TEST_PASSWORD_FILE")
    if not password_file:
        pytest.skip("ANALYTICS_TEST_PASSWORD_FILE must name the owner password file")
    for variable in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER"):
        monkeypatch.delenv(variable, raising=False)
    options = replace(
        settings.load(), host=host, database=name, password_file=password_file
    )
    first = database.connect(options)
    second = database.connect(options)
    try:
        assert database.migrate(first, options)
        before = first.execute(
            "SELECT * FROM meta.migration ORDER BY version"
        ).fetchall()
        assert database.migrate(first, options) == ()
        assert (
            first.execute("SELECT * FROM meta.migration ORDER BY version").fetchall()
            == before
        )
        first.execute("SELECT pg_advisory_lock(62001)")
        second.execute("SET statement_timeout = '200ms'")
        try:
            with pytest.raises(Exception, match="statement timeout"):
                database.migrate(second, options)
        finally:
            first.execute("SELECT pg_advisory_unlock(62001)")
        second.execute("SET statement_timeout = 0")
        assert database.migrate(second, options) == ()
        directory = tmp_path / "data" / "migrations"
        directory.mkdir(parents=True)
        (directory / "9001_good.sql").write_text(
            "CREATE TABLE meta.phase2_good (id int)"
        )
        bad = directory / "9002_bad.sql"
        bad.write_text("CREATE TABLE meta.phase2_bad (id int); SELECT 1/0;")
        monkeypatch.setattr(database, "files", answer(tmp_path))
        with pytest.raises(Exception, match="division by zero"):
            database.migrate(first, options)
        assert first.execute("SELECT to_regclass('meta.phase2_good')").fetchall() == [
            ("meta.phase2_good",)
        ]
        assert first.execute("SELECT to_regclass('meta.phase2_bad')").fetchall() == [
            (None,)
        ]
        assert first.execute(
            "SELECT version FROM meta.migration WHERE version >= 9000"
        ).fetchall() == [(9001,)]
        bad.write_text("CREATE TABLE meta.phase2_bad (id int)")
        assert len(database.migrate(first, options)) == 1
        unchanged = first.execute(
            "SELECT * FROM meta.migration ORDER BY version"
        ).fetchall()
        assert database.migrate(first, options) == ()
        assert (
            first.execute("SELECT * FROM meta.migration ORDER BY version").fetchall()
            == unchanged
        )
    finally:
        second.close()
        first.close()
