"""Runner-owned receipts and action keys; never a cache of PR state.

Opening a missing or damaged store fails closed. The runner can explicitly
rebuild it and must reconcile after observing KEYS_LOST. All transactions use
one connection; a database error closes it rather than exposing partial reads.
Times are non-negative integer Unix seconds supplied by the runner.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from tbhbdev.alpha.loop import events

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from tbhbdev.alpha.loop.rules import Rule


class ErrorCode(StrEnum):
    MISSING = "missing"
    EXISTS = "exists"
    IO = "io"
    CORRUPT = "corrupt"
    CLOSED = "closed"
    INVALID = "invalid"
    TRANSITION = "transition"
    LOCKED = "locked"


class StoreError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class TransitionError(StoreError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.TRANSITION)


class OpenMode(StrEnum):
    OPEN = "open"
    CREATE = "create"
    REBUILD = "rebuild"


class Recovery(StrEnum):
    EXISTING = "existing"
    NEW = "new"
    KEYS_LOST = "keys_lost"


class ActionState(StrEnum):
    INTENT = "intent"
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"
    SHADOW = "shadow"


class Result(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    REFUSED = "refused"
    AGREE = "agree"
    DISAGREE = "disagree"
    NO_CHANGE = "no_change"


class Outcome(StrEnum):
    LIVE = "live"
    APPROVE = "approve"
    COMMENT = "comment"
    CANNOT_REVIEW = "cannot_review"
    FAILED = "failed"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class DeliveryStatus(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"


_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_PR = rf"{events.REPOSITORY}#[1-9][0-9]{{0,9}}"
_HEAD = rf"{_PR}@{events.SHA}"
_REASON = (
    r"(?:unsigned|dirty|behind|age|action_failed|retry_failed|review_declined"
    r"|ci_red|draft)"
)
_KEY = (
    rf"(?:sweep:[0-9]{{1,19}}|(?:review|approve|merge|rerun):{_HEAD}|verify:{_PR}"
    rf"|follow:{_PR}:{_IDENTIFIER}|notify:{_HEAD}:{_REASON}"
    rf"|(?:collect|retry|stop):{_IDENTIFIER}"
    rf"|escalate:(?:{_HEAD}|{_IDENTIFIER}):{_REASON})"
)
_TRANSITIONS = {
    ActionState.INTENT: frozenset({ActionState.STARTED}),
    ActionState.STARTED: frozenset({ActionState.DONE, ActionState.FAILED}),
    ActionState.SHADOW: frozenset({ActionState.SHADOW}),
}
_RESULTS = {
    ActionState.INTENT: frozenset({None}),
    ActionState.STARTED: frozenset({None}),
    ActionState.DONE: frozenset({Result.OK}),
    ActionState.FAILED: frozenset({Result.ERROR, Result.TIMEOUT, Result.REFUSED}),
    ActionState.SHADOW: frozenset(
        {None, Result.AGREE, Result.DISAGREE, Result.NO_CHANGE}
    ),
}
_SCHEMA = """
CREATE TABLE receipts (
    delivery_id TEXT PRIMARY KEY, time INTEGER NOT NULL,
    event TEXT NOT NULL, repository TEXT NOT NULL, action TEXT NOT NULL,
    sender_login TEXT NOT NULL, pr_numbers TEXT NOT NULL, head_sha TEXT NOT NULL,
    check_name TEXT, conclusion TEXT, review_state TEXT
) STRICT;
CREATE TABLE actions (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(
        state IN ('intent', 'started', 'done', 'failed', 'shadow')),
    intent_time INTEGER NOT NULL, started_time INTEGER, finished_time INTEGER,
    result TEXT CHECK(result IN ('ok', 'error', 'timeout', 'refused',
                                'agree', 'disagree', 'no_change')),
    CHECK (
        (state IN ('intent', 'started') AND result IS NULL) OR
        (state = 'done' AND result IS NOT NULL AND result = 'ok') OR
        (state = 'failed' AND result IS NOT NULL
         AND result IN ('error', 'timeout', 'refused')) OR
        (state = 'shadow' AND (result IS NULL
         OR result IN ('agree', 'disagree', 'no_change')))
    )
) STRICT;
CREATE TABLE runs (
    name TEXT PRIMARY KEY, host TEXT NOT NULL, harness TEXT NOT NULL,
    model TEXT NOT NULL, key TEXT UNIQUE NOT NULL REFERENCES actions(key),
    outcome TEXT NOT NULL
) STRICT;
CREATE TABLE escalations (
    key TEXT PRIMARY KEY REFERENCES actions(key), marker TEXT UNIQUE NOT NULL,
    time INTEGER NOT NULL
) STRICT;
CREATE TABLE shadow_intents (
    key TEXT NOT NULL REFERENCES actions(key),
    repository TEXT NOT NULL, rule TEXT NOT NULL, PRIMARY KEY (key, repository)
) STRICT;
PRAGMA user_version = 3;
"""
# Keep SQL beside the schema. SQLite keywords and identifiers ignore case.
_SQL = {
    "receipt": (
        "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(delivery_id) DO NOTHING"
    ),
    "intent": (
        "INSERT INTO actions VALUES (?, ?, ?, NULL, NULL, NULL) "
        "ON CONFLICT(key) DO UPDATE SET state = excluded.state, "
        "intent_time = excluded.intent_time, started_time = NULL, "
        "finished_time = NULL, result = NULL "
        "WHERE actions.state = 'shadow' AND excluded.state = 'intent'"
    ),
    "action": "SELECT * FROM actions WHERE key = ?",
    "transition": (
        "UPDATE actions SET state = ?, started_time = ?, finished_time = ?, "
        "result = ? WHERE key = ? AND state = ?"
    ),
    "shadow_intent": (
        "INSERT INTO shadow_intents SELECT ?, ?, ? WHERE EXISTS "
        "(SELECT 1 FROM actions WHERE key = ? AND state = 'shadow') "
        "ON CONFLICT(key, repository) DO NOTHING"
    ),
    "shadow_records": (
        "SELECT shadow_intents.key, repository, rule FROM shadow_intents "
        "JOIN actions USING (key) WHERE actions.state = 'shadow' "
        "ORDER BY key, repository"
    ),
    "crashed": (
        "SELECT key FROM actions WHERE state = 'intent' "
        "AND started_time IS NULL ORDER BY key"
    ),
    "keys": "SELECT key FROM actions WHERE state != 'shadow'",
    "shadow_keys": "SELECT key FROM actions WHERE state = 'shadow'",
    "unfinished": "SELECT key FROM actions WHERE state = 'started' ORDER BY key",
    "run": (
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) "
        "DO UPDATE SET outcome = excluded.outcome WHERE "
        "runs.host = excluded.host AND runs.harness = excluded.harness AND "
        "runs.model = excluded.model AND runs.key = excluded.key"
    ),
    "changes": "SELECT changes()",
    "escalation": (
        "INSERT INTO escalations VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING"
    ),
    "action_counts": "SELECT key, state FROM actions",
    "run_counts": (
        "SELECT runs.key, actions.state FROM runs "
        "JOIN actions ON runs.key = actions.key"
    ),
    "integrity": "PRAGMA integrity_check",
    "version": "PRAGMA user_version",
}
_SETTINGS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = FULL",
)
_TABLE_CHECKS = (
    (
        "SELECT delivery_id, time, event, repository, action, sender_login, "
        "pr_numbers, head_sha, check_name, conclusion, review_state "
        "FROM receipts LIMIT 0"
    ),
    (
        "SELECT key, state, intent_time, started_time, finished_time, result "
        "FROM actions LIMIT 0"
    ),
    "SELECT name, host, harness, model, key, outcome FROM runs LIMIT 0",
    "SELECT key, marker, time FROM escalations LIMIT 0",
    "SELECT key, repository, rule FROM shadow_intents LIMIT 0",
)
type _ActionRow = tuple[str, str, int, int | None, int | None, str | None]


def _text(value: object, pattern: str) -> None:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise StoreError(ErrorCode.INVALID)


def _time(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise StoreError(ErrorCode.INVALID)


def _enum(value: object, kind: type[StrEnum]) -> None:
    if not isinstance(value, kind):
        raise StoreError(ErrorCode.INVALID)


def _family(key: str) -> str:
    return re.sub(rf"@{events.SHA}", "", key)


@dataclass(frozen=True)
class Receipt:
    delivery_id: str
    time: int
    event: events.Event

    def __post_init__(self) -> None:
        _text(self.delivery_id, events.DELIVERY)
        _time(self.time)
        event = self.event
        _text(event.repository, events.REPOSITORY)
        _text(event.head_sha, events.SHA)
        _text(event.sender_login, events.LOGIN)
        _text(event.event, "|".join(events.ACTIONS))
        _text(event.action, "|".join(sorted(events.ACTIONS[event.event])))
        if not isinstance(event.pr_numbers, tuple):
            raise StoreError(ErrorCode.INVALID)
        for number in event.pr_numbers:
            if type(number) is not int or not 0 < number <= 2**31 - 1:
                raise StoreError(ErrorCode.INVALID)
        for value, pattern in (
            (event.check_name, events.CHECK_NAME),
            (event.conclusion, "|".join(sorted(events.CONCLUSIONS))),
            (event.review_state, "|".join(sorted(events.REVIEW_STATES))),
        ):
            if value is not None:
                _text(value, pattern)


@dataclass(frozen=True)
class Action:
    key: str
    state: ActionState
    intent_time: int
    started_time: int | None
    finished_time: int | None
    result: Result | None


@dataclass(frozen=True)
class ShadowIntent:
    key: str
    repository: str
    rule: Rule


@dataclass(frozen=True)
class RunRecord:
    name: str
    host: str
    harness: Literal["codex", "claude"]
    model: str
    key: str
    outcome: Outcome

    def __post_init__(self) -> None:
        for value in (self.name, self.host, self.model):
            _text(value, _IDENTIFIER)
        _text(self.key, _KEY)
        if self.harness not in {"codex", "claude"}:
            raise StoreError(ErrorCode.INVALID)
        _enum(self.outcome, Outcome)


@dataclass(frozen=True)
class FamilyCount:
    family: str
    failures: int


@dataclass(frozen=True)
class Counts:
    families: tuple[FamilyCount, ...]
    reviewer_runs: int
    retries: int

    @property
    def action_failures(self) -> tuple[int, ...]:
        return tuple(item.failures for item in self.families)


def count_rows(
    object_key: str,
    actions: Iterable[tuple[str, str]],
    runs: Iterable[tuple[str, str]],
) -> Counts:
    """Aggregate validated action and run rows without reading the store.

    retries is a presence flag: unique action keys allow retry:<object> once.
    """
    failures: dict[str, int] = {}
    for key, state in actions:
        family = _family(key)
        if state == ActionState.FAILED and family.split(":")[1] == object_key:
            failures[family] = failures.get(family, 0) + 1
    review_count = retry_count = 0
    for key, state in runs:
        if state == ActionState.SHADOW:
            continue
        if _family(key) == f"review:{object_key}":
            review_count += 1
        if key == f"retry:{object_key}":
            retry_count = 1
    return Counts(
        tuple(FamilyCount(k, v) for k, v in sorted(failures.items())),
        review_count,
        retry_count,
    )


def stopped_objects_from_keys(keys: Iterable[str]) -> frozenset[str]:
    """Project live stop and escalation keys into rules.Inputs object keys."""
    objects: set[str] = set()
    for key in keys:
        if key.startswith("escalate:"):
            objects.add(key.split(":")[1])
        elif key.startswith("stop:"):
            objects.add(key.removeprefix("stop:"))
    return frozenset(objects)


def _regular_file(path: Path, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StoreError(ErrorCode.IO)  # noqa: TRY301 -- release the descriptor
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _lock(path: Path) -> int:
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
        raise StoreError(ErrorCode.IO)
    descriptor = _regular_file(Path(f"{path}.lock"), os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise StoreError(ErrorCode.LOCKED) from None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _prepare(path: Path, mode: OpenMode) -> int:
    if mode == OpenMode.REBUILD:
        for file in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            file.unlink(missing_ok=True)
    flags = 0 if mode == OpenMode.OPEN else os.O_CREAT | os.O_EXCL
    return _regular_file(path, flags)


class Store:
    def __init__(
        self, path: Path | Literal[":memory:"], *, mode: OpenMode = OpenMode.OPEN
    ) -> None:
        _enum(mode, OpenMode)
        self.recovery = {
            OpenMode.OPEN: Recovery.EXISTING,
            OpenMode.CREATE: Recovery.NEW,
            OpenMode.REBUILD: Recovery.KEYS_LOST,
        }[mode]
        self._connection: sqlite3.Connection | None = None
        self._lock_descriptor: int | None = None
        try:
            self._open(path, mode)
            with self._transaction() as db:
                for statement in _SETTINGS:
                    db.execute(statement)
                if db.execute(_SQL["integrity"]).fetchall() != [("ok",)]:
                    raise StoreError(ErrorCode.CORRUPT)  # noqa: TRY301 -- close on every failed open
                if path == ":memory:" or mode != OpenMode.OPEN:
                    db.executescript(_SCHEMA)
                if db.execute(_SQL["version"]).fetchone() != (3,):
                    raise StoreError(ErrorCode.CORRUPT)  # noqa: TRY301 -- close on every failed open
                for statement in _TABLE_CHECKS:
                    db.execute(statement)
        except FileNotFoundError:
            self.close()
            raise StoreError(ErrorCode.MISSING) from None
        except FileExistsError:
            self.close()
            raise StoreError(ErrorCode.EXISTS) from None
        except OSError:
            self.close()
            raise StoreError(ErrorCode.IO) from None
        except sqlite3.Error:
            self.close()
            raise StoreError(ErrorCode.CORRUPT) from None
        except StoreError:
            self.close()
            raise

    def _open(self, path: Path | Literal[":memory:"], mode: OpenMode) -> None:
        if path != ":memory:":
            path = Path(path)
            self._lock_descriptor = _lock(path)
            descriptor = _prepare(path, mode)
            try:
                self._connection = sqlite3.connect(path)
                opened = os.fstat(descriptor)
                connected = path.lstat()
                if (opened.st_dev, opened.st_ino) != (
                    connected.st_dev,
                    connected.st_ino,
                ):
                    raise StoreError(ErrorCode.IO)
            finally:
                os.close(descriptor)
        else:
            self._connection = sqlite3.connect(path)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._lock_descriptor is not None:
            os.close(self._lock_descriptor)
            self._lock_descriptor = None

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Generator[sqlite3.Connection]:
        connection = self._connection
        if connection is None:
            raise StoreError(ErrorCode.CLOSED)
        try:
            with connection:
                if immediate:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        except sqlite3.IntegrityError:
            raise StoreError(ErrorCode.INVALID) from None
        except sqlite3.OperationalError:
            self.close()
            raise StoreError(ErrorCode.IO) from None
        except sqlite3.Error:
            self.close()
            raise StoreError(ErrorCode.CORRUPT) from None
        except StoreError as error:
            if error.code == ErrorCode.CORRUPT:
                self.close()
            raise

    def record_receipt(self, receipt: Receipt) -> DeliveryStatus:
        """Persist a delivery. DUPLICATE means seen, not processed."""
        event = receipt.event
        with self._transaction() as db:
            result = db.execute(
                _SQL["receipt"],
                (
                    receipt.delivery_id,
                    receipt.time,
                    event.event,
                    event.repository,
                    event.action,
                    event.sender_login,
                    json.dumps(event.pr_numbers),
                    event.head_sha,
                    event.check_name,
                    event.conclusion,
                    event.review_state,
                ),
            )
            return (
                DeliveryStatus.RECORDED if result.rowcount else DeliveryStatus.DUPLICATE
            )

    def record_intent(self, key: str, time: int, *, shadow: bool = False) -> bool:
        """Claim a key, promoting a shadow row when the rule becomes live."""
        _text(key, _KEY)
        _time(time)
        if type(shadow) is not bool:
            raise StoreError(ErrorCode.INVALID)
        state = ActionState.SHADOW if shadow else ActionState.INTENT
        with self._transaction() as db:
            result = db.execute(
                _SQL["intent"],
                (key, state, time),
            )
            return result.rowcount == 1

    def action(self, key: str) -> Action | None:
        _text(key, _KEY)
        with self._transaction() as db:
            return self._action(db, key)

    @staticmethod
    def _action(db: sqlite3.Connection, key: str) -> Action | None:
        row = db.execute(_SQL["action"], (key,)).fetchone()
        if row is None:
            return None
        key, state, intent, started, finished, result = cast(
            "_ActionRow",  # pragma: no mutate (typing only)
            row,
        )
        try:
            action = Action(
                key,
                ActionState(state),
                intent,
                started,
                finished,
                None if result is None else Result(result),
            )
        except ValueError:
            raise StoreError(ErrorCode.CORRUPT) from None
        if action.result not in _RESULTS[action.state]:
            raise StoreError(ErrorCode.CORRUPT)
        return action

    def transition(
        self, key: str, state: ActionState, time: int, result: Result | None = None
    ) -> None:
        _enum(state, ActionState)
        _text(key, _KEY)
        _time(time)
        if result is not None:
            _enum(result, Result)
        if result not in _RESULTS[state]:
            raise TransitionError
        with self._transaction(immediate=True) as db:
            previous = self._action(db, key)
            if previous is None or state not in _TRANSITIONS.get(
                previous.state, frozenset()
            ):
                raise TransitionError
            last = max(
                previous.intent_time,
                previous.started_time or 0,
                previous.finished_time or 0,
            )
            if time < last:
                raise TransitionError
            updated = db.execute(
                _SQL["transition"],
                (
                    state,
                    time if state == ActionState.STARTED else previous.started_time,
                    time if result is not None else None,
                    result,
                    key,
                    previous.state,
                ),
            )
            if updated.rowcount != 1:
                raise TransitionError

    def record_shadow(self, intent: ShadowIntent, time: int) -> bool:
        """Atomically claim an intent and its report dimensions, without PR state."""
        _text(intent.key, _KEY)
        _text(intent.repository, events.REPOSITORY)
        _text(intent.rule, r"R(?:[0-9]|1[01])")
        _time(time)
        with self._transaction(immediate=True) as db:
            claimed = db.execute(_SQL["intent"], (intent.key, ActionState.SHADOW, time))
            db.execute(
                _SQL["shadow_intent"],
                (intent.key, intent.repository, intent.rule, intent.key),
            )
            return claimed.rowcount == 1

    def shadow_intents(self) -> tuple[ShadowIntent, ...]:
        with self._transaction() as db:
            return tuple(
                ShadowIntent(*row) for row in db.execute(_SQL["shadow_records"])
            )

    def score_shadow(self, key: str, time: int, result: Result) -> bool:
        """Score once; no_change stays provisional for a later reconcile read.

        The read and conditional update share an immediate transaction, as in
        transition. Final scores cannot be overwritten or counted twice.
        """
        _text(key, _KEY)
        _time(time)
        _enum(result, Result)
        if result not in {Result.AGREE, Result.DISAGREE, Result.NO_CHANGE}:
            raise TransitionError
        with self._transaction(immediate=True) as db:
            previous = self._action(db, key)
            if previous is None or previous.state != ActionState.SHADOW:
                raise TransitionError
            if time < max(previous.intent_time, previous.finished_time or 0):
                raise TransitionError
            if previous.result in {Result.AGREE, Result.DISAGREE}:
                return False
            updated = db.execute(
                _SQL["transition"],
                (ActionState.SHADOW, None, time, result, key, ActionState.SHADOW),
            )
            if updated.rowcount != 1:
                raise TransitionError
            return True

    def crashed_intents(self) -> tuple[str, ...]:
        with self._transaction() as db:
            return tuple(row[0] for row in db.execute(_SQL["crashed"]))

    def existing_keys(self) -> frozenset[str]:
        """Keys that suppress live actions; shadow observations never do."""
        with self._transaction() as db:
            return frozenset(row[0] for row in db.execute(_SQL["keys"]))

    def shadow_keys(self) -> frozenset[str]:
        with self._transaction() as db:
            return frozenset(row[0] for row in db.execute(_SQL["shadow_keys"]))

    def unfinished_starts(self) -> tuple[str, ...]:
        """Started keys needing reconciliation after a crash before completion."""
        with self._transaction() as db:
            return tuple(row[0] for row in db.execute(_SQL["unfinished"]))

    def stopped_objects(self) -> frozenset[str]:
        return stopped_objects_from_keys(self.existing_keys())

    def record_run(self, run: RunRecord) -> None:
        """Record observations of a named run, preserving its immutable identity."""
        with self._transaction() as db:
            db.execute(
                _SQL["run"],
                (run.name, run.host, run.harness, run.model, run.key, run.outcome),
            )
            if db.execute(_SQL["changes"]).fetchone() != (1,):
                raise StoreError(ErrorCode.INVALID)

    def record_escalation(self, key: str, time: int) -> tuple[str, bool]:
        _text(key, rf"escalate:(?:{_HEAD}|{_IDENTIFIER}):{_REASON}")
        _time(time)
        marker = f"<!-- loop:{key} -->"
        with self._transaction() as db:
            inserted = db.execute(
                _SQL["escalation"],
                (key, marker, time),
            )
            return marker, inserted.rowcount == 1

    def counts(self, object_key: str) -> Counts:
        """PR counts span heads; run retry counts come from retry action keys.

        Families retain action kind and reason/step while dropping the head SHA.
        Only failed actions count. Shadow keys never consume retry/review counts.
        Review counts include every verdict: the runner combines them with
        approvals from reads to supply reviewer_runs_without_approval to rules.
        """
        _text(object_key, rf"(?:{_PR}|{_IDENTIFIER})")
        with self._transaction() as db:
            return count_rows(
                object_key,
                db.execute(_SQL["action_counts"]),
                db.execute(_SQL["run_counts"]),
            )
