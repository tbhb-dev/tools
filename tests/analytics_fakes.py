"""Recorded database operations with transaction rollback for analytics tests."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from importlib.resources.abc import Traversable


@dataclass
class Rows:
    values: list[tuple[object, ...]] = field(default_factory=list)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.values


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Sequence[object] | None]] = []
        self.events: list[str] = []
        self.versions: list[int] = []
        self.exists = False
        self.roles: set[str] = set()
        self.catalog: object = '{"schemas": [], "relations": [], "functions": []}'
        self.failure = ""
        self.closed = False

    def execute(
        self, statement: str, parameters: Sequence[object] | None = None
    ) -> Rows:
        self.calls.append((statement, parameters))
        if self.failure and self.failure in statement:
            message = "syntax error containing credential sentinel"
            raise ValueError(message)
        if statement.startswith("CREATE TABLE IF NOT EXISTS meta.migration"):
            self.exists = True
        if statement == "SELECT to_regclass('meta.migration')":
            return Rows([("meta.migration" if self.exists else None,)])
        if statement == "SELECT version FROM meta.migration":
            return Rows([(version,) for version in self.versions])
        if statement.startswith("SELECT 1 FROM pg_roles"):
            assert parameters is not None
            return Rows([(1,)] if parameters[0] in self.roles else [])
        if statement.startswith("INSERT INTO meta.migration"):
            assert parameters is not None
            assert isinstance(parameters[0], int)
            self.versions.append(parameters[0])
        if statement.startswith("WITH schemas AS"):
            return Rows([(self.catalog,)])
        return Rows()

    @contextmanager
    def transaction(self) -> Generator[None]:
        self.events.append("begin")
        versions = self.versions.copy()
        try:
            yield
        except ValueError:
            self.versions = versions
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def close(self) -> None:
        self.closed = True


def answer[T](value: T) -> Callable[..., T]:
    def respond(*_args: object, **_kwargs: object) -> T:
        return value

    return respond


@dataclass
class Resource:
    path: Traversable
    parts: tuple[str, ...]

    def joinpath(self, *parts: str) -> Traversable:
        # Check case explicitly even when the filesystem ignores it.
        assert parts == self.parts
        return self.path


def packaged(path: Traversable, *parts: str) -> Callable[[str], Resource]:
    def locate(package: str) -> Resource:
        assert package == "tbhbdev.analytics"
        return Resource(path, parts)

    return locate
