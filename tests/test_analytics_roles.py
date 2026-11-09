"""The role limits and privilege matrix match the shipped migration."""

import pytest

from tbhbdev.analytics.decisions.roles import GRANTS, ROLES
from tbhbdev.analytics.model import Role


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
