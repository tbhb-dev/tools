"""Login role limits and SQL privilege statements, without passwords."""

import re

from tbhbdev.analytics.model import Role, Settings

ROLES = (
    Role("analytics_loader"),
    Role("analytics_reader"),
    Role("grafana_reader", connection_limit=5, statement_timeout="10s"),
)

GRANTS = (
    "REVOKE CONNECT, TEMPORARY ON DATABASE analytics FROM PUBLIC;",
    (
        "GRANT CONNECT ON DATABASE analytics TO analytics_loader, "
        "analytics_reader, grafana_reader;"
    ),
    "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
    "GRANT USAGE ON SCHEMA raw, meta, model TO analytics_loader;",
    "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA raw TO analytics_loader;",
    "GRANT SELECT, INSERT, UPDATE ON meta.load TO analytics_loader;",
    "GRANT MAINTAIN ON ALL TABLES IN SCHEMA model TO analytics_loader;",
    "GRANT USAGE ON SCHEMA staging, model, metric, mart, meta TO analytics_reader;",
    (
        "GRANT SELECT ON ALL TABLES IN SCHEMA staging, model, metric, mart, "
        "meta TO analytics_reader;"
    ),
    "GRANT USAGE ON SCHEMA mart TO grafana_reader;",
    "GRANT SELECT ON ALL TABLES IN SCHEMA mart TO grafana_reader;",
    (
        "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA raw "
        "GRANT SELECT, INSERT, UPDATE ON TABLES TO analytics_loader;"
    ),
    (
        "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA model "
        "GRANT MAINTAIN ON TABLES TO analytics_loader;"
    ),
    (
        "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA "
        "staging, model, metric, mart, meta GRANT SELECT ON TABLES TO "
        "analytics_reader;"
    ),
    (
        "ALTER DEFAULT PRIVILEGES FOR ROLE analytics_owner IN SCHEMA mart "
        "GRANT SELECT ON TABLES TO grafana_reader;"
    ),
)


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def selected(settings: Settings) -> tuple[tuple[Role, str], ...]:
    result = (
        (Role(settings.reader_role), "reader"),
        (Role(settings.grafana_role, 5, "10s"), "grafana_reader"),
    )
    if settings.loader_enabled:
        return ((Role(settings.loader_role), "loader"), *result)
    return result


def password_file(settings: Settings, key: str) -> tuple[str, str | None]:
    """Return the config key that names a role's password file and its value."""
    name = {
        "loader": "loader_password_file",
        "reader": "reader_password_file",
        "grafana_reader": "grafana_password_file",
    }[key]
    return name, getattr(settings, name)


def passwordless(settings: Settings) -> tuple[tuple[str, str], ...]:
    """Return each selected role without a configured password file and the key."""
    result: list[tuple[str, str]] = []
    for role, key in selected(settings):
        name, path = password_file(settings, key)
        if path is None:
            result.append((role.name, name))
    return tuple(result)


def statements(settings: Settings) -> tuple[str, ...]:
    replacements = {
        "analytics": settings.database,
        "analytics_owner": settings.owner_role,
        "analytics_loader": settings.loader_role,
        "analytics_reader": settings.reader_role,
        "grafana_reader": settings.grafana_role,
    }
    result: list[str] = []
    for grant in GRANTS:
        statement = grant
        if not settings.loader_enabled:
            if statement == GRANTS[1]:
                statement = statement.replace("analytics_loader, ", "")
            elif "analytics_loader" in statement:
                continue
        result.append(
            re.sub(
                r"\b(?:analytics|analytics_owner|analytics_loader|analytics_reader|grafana_reader)\b",
                lambda match: identifier(replacements[match[0]]),
                statement,
            )
        )
    return tuple(result)
