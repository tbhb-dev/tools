"""Opt-in assertions against a caller-provided disposable PostgreSQL database."""

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
    options = replace(settings.load(), host=host)
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
