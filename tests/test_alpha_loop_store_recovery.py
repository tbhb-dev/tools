"""Failure recovery and promotion use synthetic local data only."""

# ruff: noqa: SLF001 -- exercise SQLite failure boundaries and lifetime resources

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from test_alpha_loop_store import BASE, HEAD, KEY

from tbhbdev.alpha.loop import rules, store
from tbhbdev.alpha.loop.store import (
    Action,
    ActionState,
    Counts,
    ErrorCode,
    FamilyCount,
    OpenMode,
    Result,
    Store,
    StoreError,
    TransitionError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def database() -> Iterator[Store]:
    database = Store(":memory:")
    yield database
    database.close()


def test_shadow_keys_do_not_suppress_live_decisions(database: Store) -> None:
    assert database.record_intent(KEY, 1, shadow=True)
    assert database.existing_keys() == frozenset()
    assert database.shadow_keys() == frozenset({KEY})
    assert database.record_intent(KEY, 2)
    assert database.action(KEY) == Action(KEY, ActionState.INTENT, 2, None, None, None)
    assert database.existing_keys() == frozenset({KEY})
    assert database.shadow_keys() == frozenset()
    assert not database.record_intent(KEY, 3)


def test_scored_shadow_promotion_resets_result_and_times(database: Store) -> None:
    assert database.record_intent(KEY, 1, shadow=True)
    database.transition(KEY, ActionState.SHADOW, 2, Result.AGREE)
    assert database.action(KEY) == Action(
        KEY, ActionState.SHADOW, 1, None, 2, Result.AGREE
    )
    assert database.record_intent(KEY, 3)
    assert database.action(KEY) == Action(KEY, ActionState.INTENT, 3, None, None, None)


@pytest.mark.parametrize("mode", list(OpenMode))
def test_second_opener_refused_including_rebuild(
    tmp_path: Path, mode: OpenMode
) -> None:
    path = tmp_path / "example.sqlite3"
    first = Store(path, mode=OpenMode.CREATE)
    assert first.record_intent(KEY, 1)
    try:
        with pytest.raises(StoreError) as error:
            Store(path, mode=mode)
        assert error.value.code == ErrorCode.LOCKED
        assert first.existing_keys() == frozenset({KEY})
        lock_inode = Path(f"{path}.lock").stat().st_ino
    finally:
        first.close()
    rebuilt = Store(path, mode=OpenMode.REBUILD)
    try:
        assert Path(f"{path}.lock").stat().st_ino == lock_inode
        with pytest.raises(StoreError) as error:
            Store(path)
        assert error.value.code == ErrorCode.LOCKED
        assert rebuilt.existing_keys() == frozenset()
    finally:
        rebuilt.close()


def test_failed_open_releases_lock(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    with pytest.raises(StoreError):
        Store(path)
    database = Store(path, mode=OpenMode.CREATE)
    database.close()


@pytest.mark.parametrize("state", list(ActionState))
@pytest.mark.parametrize("result", [None, *Result])
def test_state_result_table(
    database: Store, state: ActionState, result: Result | None
) -> None:
    assert database.record_intent(KEY, 0, shadow=state == ActionState.SHADOW)
    if state in {ActionState.DONE, ActionState.FAILED}:
        database.transition(KEY, ActionState.STARTED, 1)
    allowed = {
        ActionState.INTENT: set(),
        ActionState.STARTED: {None},
        ActionState.DONE: {Result.OK},
        ActionState.FAILED: {Result.ERROR, Result.TIMEOUT, Result.REFUSED},
        ActionState.SHADOW: {None, Result.AGREE, Result.DISAGREE, Result.NO_CHANGE},
    }
    before = database.action(KEY)
    if result in allowed[state]:
        database.transition(KEY, state, 2, result)
        action = database.action(KEY)
        assert action is not None
        assert action.state == state
        assert action.result == result
    else:
        with pytest.raises(TransitionError):
            database.transition(KEY, state, 2, result)
        assert database.action(KEY) == before


def test_unfinished_starts_exclude_terminal_and_shadow(database: Store) -> None:
    assert database.unfinished_starts() == ()
    assert database.record_intent(KEY, 0)
    database.transition(KEY, ActionState.STARTED, 1)
    other = f"merge:{BASE}@{HEAD}"
    assert database.record_intent(other, 0)
    database.transition(other, ActionState.STARTED, 1)
    assert database.unfinished_starts() == (other, KEY)
    database.transition(other, ActionState.DONE, 2, Result.OK)
    assert database.record_intent("sweep:1", 0, shadow=True)
    assert database.record_intent("sweep:2", 0)
    assert database.unfinished_starts() == (KEY,)
    assert database.crashed_intents() == ("sweep:2",)


def test_stopped_objects_feed_rules_inputs(database: Store) -> None:
    obj = f"{BASE}@{HEAD}"
    for key in (f"escalate:{obj}:age", "stop:example-run"):
        assert database.record_intent(key, 0)
    assert database.record_intent("escalate:example-shadow:age", 0, shadow=True)
    assert database.stopped_objects() == frozenset({obj, "example-run"})
    budget = rules.SpawnBudget(1, 0, 1, 1, 0, (), ())
    inputs = rules.Inputs(
        budget,
        runs=(rules.Run("example-run", "codex", "finished", 0, 10),),
        stopped_objects=database.stopped_objects(),
    )
    assert rules.decide(inputs) == ()


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ((), frozenset()),
        ((KEY, "retry:example-run"), frozenset()),
        (("escalate:example-run:age",), frozenset({"example-run"})),
        ((f"escalate:{BASE}@{HEAD}:dirty",), frozenset({f"{BASE}@{HEAD}"})),
        (("stop:example-run", "escalate:example-run:age"), frozenset({"example-run"})),
    ],
)
def test_stopped_object_key_table(
    keys: tuple[str, ...], expected: frozenset[str]
) -> None:
    assert store.stopped_objects_from_keys(keys) == expected


@pytest.mark.parametrize(
    ("actions", "runs", "expected"),
    [
        ((), (), Counts((), 0, 0)),
        (((KEY, "failed"),), (), Counts((FamilyCount(f"review:{BASE}", 1),), 0, 0)),
        (((KEY, "done"),), (), Counts((), 0, 0)),
        ((("sweep:1", "failed"),), (), Counts((), 0, 0)),
        (
            ((KEY, "failed"),),
            ((KEY, "shadow"),),
            Counts((FamilyCount(f"review:{BASE}", 1),), 0, 0),
        ),
        ((), ((KEY, "started"), (f"merge:{BASE}@{HEAD}", "done")), Counts((), 1, 0)),
        ((), (("retry:example-run", "started"),), Counts((), 0, 0)),
    ],
)
def test_counts_table(
    actions: tuple[tuple[str, str], ...],
    runs: tuple[tuple[str, str], ...],
    expected: Counts,
) -> None:
    assert store.count_rows(BASE, actions, runs) == expected


def test_escalation_reports_creation(database: Store) -> None:
    key = "escalate:example-run:retry_failed"
    assert database.record_intent(key, 0)
    marker = f"<!-- loop:{key} -->"
    assert database.record_escalation(key, 1) == (marker, True)
    assert database.record_escalation(key, 2) == (marker, False)


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_nonregular_database_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "example.sqlite3"
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    original = os.open

    def guarded_open(path: Path, flags: int, mode: int = 0o777) -> int:
        assert flags & os.O_NONBLOCK
        return original(path, flags, mode)

    monkeypatch.setattr(store.os, "open", guarded_open)
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == ErrorCode.IO


@pytest.mark.parametrize("kind", ["symlink", "group", "world", "file"])
def test_unsafe_parent_is_rejected(tmp_path: Path, kind: str) -> None:
    parent = tmp_path / "example-parent"
    if kind == "symlink":
        parent.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "file":
        parent.touch()
    else:
        parent.mkdir(mode=0o700)
        parent.chmod(0o710 if kind == "group" else 0o701)
    with pytest.raises(StoreError) as error:
        Store(parent / "example.sqlite3", mode=OpenMode.CREATE)
    assert error.value.code == ErrorCode.IO


def test_inode_changed_during_connect_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "example.sqlite3"
    original = store.sqlite3.connect

    def connect(path: Path) -> sqlite3.Connection:
        connection = original(path)
        path.rename(path.with_suffix(".old"))
        path.touch()
        return connection

    monkeypatch.setattr(store.sqlite3, "connect", connect)
    with pytest.raises(StoreError) as error:
        Store(path, mode=OpenMode.CREATE)
    assert error.value.code == ErrorCode.IO
    monkeypatch.setattr(store.sqlite3, "connect", original)
    rebuilt = Store(path, mode=OpenMode.REBUILD)
    rebuilt.close()


def test_database_locked_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    try:
        with database._transaction() as connection:
            connection.execute("PRAGMA busy_timeout = 1")
        with closing(sqlite3.connect(path)) as competing, competing:
            competing.execute("BEGIN IMMEDIATE")
            with pytest.raises(StoreError) as error:
                database.record_intent(KEY, 0)
            assert error.value.code == ErrorCode.IO
        with pytest.raises(StoreError) as error:
            database.existing_keys()
        assert error.value.code == ErrorCode.CLOSED
    finally:
        database.close()


def test_concurrent_transitions_finish_once(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    assert database.record_intent(KEY, 0)
    database.transition(KEY, ActionState.STARTED, 1)
    barrier = Barrier(2)

    def finish(_index: int) -> str:
        # Bypass the lifetime lock to exercise SQLite against a competing writer.
        writer = Store(":memory:")
        writer.close()
        writer._connection = sqlite3.connect(path)
        try:
            barrier.wait(timeout=5)
            try:
                writer.transition(KEY, ActionState.DONE, 2, Result.OK)
            except TransitionError:
                return "refused"
            return "done"
        finally:
            writer.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(finish, range(2)))
        assert sorted(results) == ["done", "refused"]
        assert database.action(KEY) == Action(KEY, ActionState.DONE, 0, 1, 2, Result.OK)
    finally:
        database.close()


def test_crash_before_commit_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    assert database.record_intent(KEY, 0)
    database.close()
    script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE actions SET state = 'started', started_time = 1")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, timeout=10)
    reopened = Store(path)
    try:
        assert reopened.action(KEY) == Action(
            KEY, ActionState.INTENT, 0, None, None, None
        )
    finally:
        reopened.close()


def test_truncated_database_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    database.close()
    path.write_bytes(path.read_bytes()[:100])
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == ErrorCode.CORRUPT


def test_checkpointed_store_survives_missing_wal(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    assert database.record_intent(KEY, 0)
    database.close()
    Path(f"{path}-wal").unlink(missing_ok=True)
    reopened = Store(path)
    try:
        assert reopened.existing_keys() == frozenset({KEY})
    finally:
        reopened.close()


def test_schema_rejects_invalid_codes_and_pairs(database: Store) -> None:
    assert database.record_intent(KEY, 0)
    for state, result in (
        ("untrusted", None),
        ("intent", "untrusted"),
        ("intent", "ok"),
        ("done", "timeout"),
        ("failed", "ok"),
        ("shadow", "ok"),
    ):
        with pytest.raises(StoreError) as error, database._transaction() as connection:
            connection.execute(
                "UPDATE actions SET state = ?, result = ? WHERE key = ?",
                (state, result, KEY),
            )
        assert error.value.code == ErrorCode.INVALID
    assert database.action(KEY) == Action(KEY, ActionState.INTENT, 0, None, None, None)


def test_full_durability_is_explicit(database: Store) -> None:
    assert "PRAGMA synchronous = FULL" in store._SETTINGS
    with database._transaction() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)


def test_flock_io_failure_releases_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = store.fcntl.flock

    def fail(_descriptor: int, _operation: int) -> None:
        raise OSError

    monkeypatch.setattr(store.fcntl, "flock", fail)
    path = tmp_path / "example.sqlite3"
    with pytest.raises(StoreError) as error:
        Store(path, mode=OpenMode.CREATE)
    assert error.value.code == ErrorCode.IO
    monkeypatch.setattr(store.fcntl, "flock", original)
    database = Store(path, mode=OpenMode.CREATE)
    database.close()


def test_guarded_update_must_change_exactly_one_row(database: Store) -> None:
    assert database.record_intent(KEY, 0)
    with database._transaction() as connection:
        connection.execute(
            "CREATE TRIGGER ignore_transition BEFORE UPDATE ON actions "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(TransitionError):
        database.transition(KEY, ActionState.STARTED, 1)
    assert database.action(KEY) == Action(KEY, ActionState.INTENT, 0, None, None, None)


def test_invalid_stored_pair_fails_closed(database: Store) -> None:
    assert database.record_intent(KEY, 0)
    with database._transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE actions SET result = 'ok'")
    with pytest.raises(StoreError) as error:
        database.action(KEY)
    assert error.value.code == ErrorCode.CORRUPT
    with pytest.raises(StoreError) as error:
        database.existing_keys()
    assert error.value.code == ErrorCode.CLOSED


def test_transition_reads_inside_immediate_transaction(database: Store) -> None:
    assert database.record_intent(KEY, 0)
    statements: list[str] = []
    with database._transaction() as connection:
        connection.set_trace_callback(statements.append)
    database.transition(KEY, ActionState.STARTED, 1)
    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[1].startswith("SELECT")
    assert "AND state = 'intent'" in statements[2]
    assert statements[3:] == ["COMMIT"]


def test_lost_uncheckpointed_wal_requires_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    script = """
import os, sys
from pathlib import Path
from tbhbdev.alpha.loop.store import Store, OpenMode
db = Store(Path(sys.argv[1]), mode=OpenMode.CREATE)
db.record_intent("sweep:1", 0)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, timeout=10)
    wal = Path(f"{path}-wal")
    assert wal.stat().st_size > 0
    wal.unlink()
    Path(f"{path}-shm").unlink()
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == ErrorCode.SCHEMA_VERSION
    rebuilt = Store(path, mode=OpenMode.REBUILD)
    try:
        assert rebuilt.existing_keys() == frozenset()
    finally:
        rebuilt.close()


def test_shadow_result_cannot_reverse_finished_time(database: Store) -> None:
    assert database.record_intent(KEY, 10, shadow=True)
    database.transition(KEY, ActionState.SHADOW, 20, Result.NO_CHANGE)
    with pytest.raises(TransitionError):
        database.transition(KEY, ActionState.SHADOW, 19, Result.AGREE)
    assert database.action(KEY) == Action(
        KEY, ActionState.SHADOW, 10, None, 20, Result.NO_CHANGE
    )


@pytest.mark.parametrize("version", [0, 1, 2, 4])
def test_prior_schema_requires_explicit_rebuild(tmp_path: Path, version: int) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    database.close()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == ErrorCode.SCHEMA_VERSION
    assert str(error.value) == f"schema version {version}; rebuild required"
    assert path.read_bytes() == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
