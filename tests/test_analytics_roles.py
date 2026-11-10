"""The role limits and privilege matrix match the shipped migration."""

import re

import pytest

from tbhbdev.analytics.decisions.roles import (
    GRANTS,
    ROLES,
    identifier,
    password_file,
    passwordless,
    selected,
    statements,
)
from tbhbdev.analytics.model import Role, Settings


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        (0, Role("analytics_loader")),
        (1, Role("analytics_reader")),
        (2, Role("grafana_reader", 5, "10s")),
    ],
)
def test_login_roles(index: int, expected: Role) -> None:
    assert ROLES[index] == expected
    assert ROLES[index].connection_limit == expected.connection_limit
    assert ROLES[index].statement_timeout == expected.statement_timeout
    assert len(ROLES) == 3


@pytest.mark.parametrize(
    ("recipient", "expected"),
    [
        (
            "PUBLIC",
            (
                "REVOKE CONNECT, TEMPORARY ON DATABASE analytics FROM PUBLIC;",
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            ),
        ),
        (
            "analytics_loader",
            (
                "GRANT USAGE ON SCHEMA raw, meta, model TO analytics_loader;",
                (
                    "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA raw "
                    "TO analytics_loader;"
                ),
                "GRANT SELECT, INSERT, UPDATE ON meta.load TO analytics_loader;",
                "GRANT MAINTAIN ON ALL TABLES IN SCHEMA model TO analytics_loader;",
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA raw "
                    "GRANT SELECT, INSERT, UPDATE ON TABLES TO analytics_loader;"
                ),
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA model "
                    "GRANT MAINTAIN ON TABLES TO analytics_loader;"
                ),
            ),
        ),
        (
            "analytics_reader",
            (
                (
                    "GRANT USAGE ON SCHEMA staging, model, metric, mart, meta "
                    "TO analytics_reader;"
                ),
                (
                    "GRANT SELECT ON ALL TABLES IN SCHEMA "
                    "staging, model, metric, mart, meta "
                    "TO analytics_reader;"
                ),
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner "
                    "IN SCHEMA staging, model, metric, mart, meta "
                    "GRANT SELECT ON TABLES TO analytics_reader;"
                ),
            ),
        ),
        (
            "grafana_reader",
            (
                "GRANT USAGE ON SCHEMA mart TO grafana_reader;",
                "GRANT SELECT ON ALL TABLES IN SCHEMA mart TO grafana_reader;",
                (
                    "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA mart "
                    "GRANT SELECT ON TABLES TO grafana_reader;"
                ),
            ),
        ),
    ],
)
def test_privilege_matrix(recipient: str, expected: tuple[str, ...]) -> None:
    assert (
        tuple(
            statement
            for statement in GRANTS
            if statement.endswith(f" {recipient};")
            and "CONNECT ON DATABASE" not in statement
        )
        == expected
    )


def test_database_connect_grant_and_statement_count() -> None:
    assert GRANTS[1] == (
        "GRANT CONNECT ON DATABASE analytics "
        "TO analytics_loader, analytics_reader, grafana_reader;"
    )
    assert len(GRANTS) == 15


@pytest.mark.parametrize("enabled", [False, True])
def test_selected_roles_and_grants(enabled: bool) -> None:

    options = Settings(loader_enabled=enabled)
    expected = (
        (Role("analytics_reader"), "reader"),
        (Role("grafana_reader", 5, "10s"), "grafana_reader"),
    )
    assert selected(options) == (
        ((Role("analytics_loader"), "loader"), *expected) if enabled else expected
    )
    expected_grants = tuple(
        line
        for line in GRANTS
        if enabled or "analytics_loader" not in line or line == GRANTS[1]
    )
    if not enabled:
        expected_grants = tuple(
            s.replace("analytics_loader, ", "") for s in expected_grants
        )

    assert statements(options) == tuple(
        re.sub(
            r"\b(analytics|analytics_owner|analytics_loader|analytics_reader|grafana_reader)\b",
            r'"\1"',
            line,
        )
        for line in expected_grants
    )


def test_custom_identifiers() -> None:

    options = Settings(
        database='db"x',
        owner_role="own",
        reader_role="read",
        grafana_role="graf",
        loader_role="load",
        loader_enabled=True,
    )
    assert identifier('a"b') == '"a""b"'
    assert selected(options) == (
        (Role("load"), "loader"),
        (Role("read"), "reader"),
        (Role("graf", 5, "10s"), "grafana_reader"),
    )
    for original, rendered in zip(GRANTS, statements(options), strict=True):
        expected = original
        for before, after in (
            ("analytics_owner", '"own"'),
            ("analytics_loader", '"load"'),
            ("analytics_reader", '"read"'),
            ("grafana_reader", '"graf"'),
            ("analytics", '"db""x"'),
        ):
            expected = expected.replace(before, after)
        assert rendered == expected


FILES = {
    "loader_password_file": "l",
    "reader_password_file": "r",
    "grafana_password_file": "g",
}


@pytest.mark.parametrize(
    ("key", "name"),
    [
        ("loader", "loader_password_file"),
        ("reader", "reader_password_file"),
        ("grafana_reader", "grafana_password_file"),
    ],
)
def test_password_file_key(key: str, name: str) -> None:
    settings = Settings(**FILES)
    assert password_file(settings, key) == (name, name[0])
    assert password_file(Settings(), key) == (name, None)


@pytest.mark.parametrize("enabled", [False, True])
def test_passwordless_lists_selected_roles_without_files(enabled: bool) -> None:
    everything = (
        ("analytics_loader", "loader_password_file"),
        ("analytics_reader", "reader_password_file"),
        ("grafana_reader", "grafana_password_file"),
    )
    assert (
        passwordless(Settings(loader_enabled=enabled))
        == everything[0 if enabled else 1 :]
    )
    assert passwordless(Settings(loader_enabled=enabled, **FILES)) == ()
    assert passwordless(
        Settings(
            loader_enabled=enabled,
            loader_role="custom_loader",
            reader_role="custom_reader",
            reader_password_file=FILES["reader_password_file"],
        )
    ) == ((("custom_loader", "loader_password_file"),) if enabled else ()) + (
        ("grafana_reader", "grafana_password_file"),
    )
