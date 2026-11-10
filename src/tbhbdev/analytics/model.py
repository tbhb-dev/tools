"""Frozen values passed between analytics decisions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = "localhost"
    port: int = 5432
    database: str = "analytics"
    user: str = "analytics_owner"
    password_file: str | None = None
    owner_role: str = "analytics_owner"
    loader_role: str = "analytics_loader"
    reader_role: str = "analytics_reader"
    grafana_role: str = "grafana_reader"


@dataclass(frozen=True)
class Role:
    name: str
    connection_limit: int | None = None
    statement_timeout: str | None = None


@dataclass(frozen=True)
class Migration:
    name: str
    version: int


@dataclass(frozen=True)
class Relation:
    name: str
    kind: str
    columns: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Catalog:
    schemas: tuple[str, ...] = ()
    relations: tuple[Relation, ...] = ()
    functions: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Verdict:
    violations: tuple[str, ...] = ()
    value: str | None = None
    message: str = ""
