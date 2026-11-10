"""Login role limits and SQL privilege statements, without passwords."""

from tbhbdev.analytics.model import Role

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
