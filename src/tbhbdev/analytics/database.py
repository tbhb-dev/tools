"""Owner connections and serialized transactional analytics migrations."""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from tbhbdev.analytics.decisions import migrations, roles, scram

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from tbhbdev.analytics.model import Migration, Settings


class Result(Protocol):
    def fetchall(self) -> list[tuple[object, ...]]: ...


class Connection(Protocol):
    def execute(
        self, statement: str, parameters: Sequence[object] | None = None
    ) -> Result: ...
    def transaction(self) -> AbstractContextManager[object]: ...
    def close(self) -> None: ...


def driver(settings: Settings, password: str) -> Connection:
    """Keep the driver import and connection model in one replaceable function."""
    import psycopg  # noqa: PLC0415

    # The type argument has no runtime effect.
    return cast(
        "Connection",  # pragma: no mutate
        psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=password,
            connect_timeout=10,
            autocommit=True,
        ),
    )


def password(path: Path) -> str:
    value = path.read_bytes().decode().partition("\n")[0]
    if re.fullmatch(r"[A-Za-z0-9]{32}", value) is None:
        message = "password file damaged"
        raise ValueError(message)
    return value


def connect(settings: Settings) -> Connection:
    if settings.user != settings.owner_role:
        message = "analytics requires the owner role as the connection user"
        raise ValueError(message)
    if settings.password_file is None:
        message = "[analytics] password_file is required"
        raise ValueError(message)
    return driver(settings, password(Path(settings.password_file)))


def migrate(
    connection: Connection, settings: Settings, *, dry_run: bool = False
) -> tuple[Migration, ...]:
    connection.execute("SELECT pg_advisory_lock(62001)")
    try:
        return _migrate(connection, settings, dry_run=dry_run)
    finally:
        connection.execute("SELECT pg_advisory_unlock(62001)")


def _migrate(
    connection: Connection, settings: Settings, *, dry_run: bool
) -> tuple[Migration, ...]:
    if not dry_run:
        connection.execute("CREATE SCHEMA IF NOT EXISTS meta")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta.migration "
            "(version integer PRIMARY KEY, applied_at timestamptz)"
        )
        _roles(connection, settings)
    exists = connection.execute("SELECT to_regclass('meta.migration')").fetchall()
    applied = (
        connection.execute("SELECT version FROM meta.migration").fetchall()
        if exists[0][0] is not None
        else []
    )
    directory = files("tbhbdev.analytics").joinpath("data", "migrations")
    pending = migrations.pending(
        (entry.name for entry in directory.iterdir() if entry.is_file()),
        # The cast changes only the static type, so its type argument cannot
        # affect runtime behavior.
        (
            cast(
                "int",  # pragma: no mutate
                row[0],
            )
            for row in applied
        ),
    )
    if not dry_run:
        for migration in pending:
            with connection.transaction():
                connection.execute(directory.joinpath(migration.name).read_text())
                connection.execute(
                    "INSERT INTO meta.migration VALUES (%s, now())",
                    (migration.version,),
                )
        for statement in roles.statements(settings):
            connection.execute(statement)
    return pending


def _roles(connection: Connection, settings: Settings) -> None:
    # The type argument has no runtime effect.
    path = Path(
        cast(
            "str",  # pragma: no mutate
            settings.password_file,
        )
    )
    directory = path.parent
    owner = password(path)
    for role, key in roles.selected(settings):
        identifier = roles.identifier(role.name)
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role.name,)
        ).fetchall()
        if not exists:
            connection.execute(f"CREATE ROLE {identifier} LOGIN")
        hashed = scram.verifier(
            password(directory / f"{key}_password"), scram.stable_salt(owner, key)
        )
        connection.execute(f"ALTER ROLE {identifier} PASSWORD '{hashed}'")
        if role.connection_limit is not None:
            connection.execute(
                f"ALTER ROLE {identifier} CONNECTION LIMIT {role.connection_limit}"
            )
        if role.statement_timeout is not None:
            connection.execute(
                f"ALTER ROLE {identifier} SET statement_timeout = "
                f"'{role.statement_timeout}'"
            )
