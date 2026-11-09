"""Synthetic receipts and local databases exercise the runner's durable boundary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev.alpha.loop import events, rules, store
from tbhbdev.alpha.loop.store import (
    Action,
    ActionState,
    Counts,
    DeliveryStatus,
    ErrorCode,
    FamilyCount,
    OpenMode,
    Outcome,
    Receipt,
    Recovery,
    Result,
    RunRecord,
    Store,
    StoreError,
    TransitionError,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import Literal

BASE = "example-org/example-repo#7"
HEAD = "a" * 40
KEY = f"review:{BASE}@{HEAD}"
EVENT = events.Event(
    "check_run",
    "example-org/example-repo",
    "completed",
    "example-user",
    (7, 9),
    HEAD,
    "example check",
    "success",
)
RECEIPT = Receipt("example-delivery", 12, EVENT)
RUN = RunRecord(
    "example-run",
    "example-host.example.com",
    "codex",
    "example-model",
    KEY,
    Outcome.LIVE,
)


def changed_run(change: dict[str, object]) -> RunRecord:
    return RunRecord(
        cast("str", change.get("name", RUN.name)),
        cast("str", change.get("host", RUN.host)),
        cast("Literal['codex', 'claude']", change.get("harness", RUN.harness)),
        cast("str", change.get("model", RUN.model)),
        cast("str", change.get("key", RUN.key)),
        cast("Outcome", change.get("outcome", RUN.outcome)),
    )


@pytest.fixture
def database() -> Generator[Store]:
    db = Store(":memory:")
    yield db
    db.close()


def advance(db: Store, state: ActionState) -> None:
    assert db.record_intent(KEY, 10, shadow=state == ActionState.SHADOW)
    if state in {ActionState.STARTED, ActionState.DONE, ActionState.FAILED}:
        db.transition(KEY, ActionState.STARTED, 11)
    if state in {ActionState.DONE, ActionState.FAILED}:
        db.transition(
            KEY, state, 12, Result.ERROR if state == ActionState.FAILED else Result.OK
        )


@pytest.mark.parametrize("before", list(ActionState))
@pytest.mark.parametrize("after", list(ActionState))
def test_every_transition(
    database: Store, before: ActionState, after: ActionState
) -> None:
    advance(database, before)
    previous = database.action(KEY)
    result = {ActionState.DONE: Result.OK, ActionState.FAILED: Result.ERROR}.get(after)
    legal = (before, after) in {
        (ActionState.INTENT, ActionState.STARTED),
        (ActionState.STARTED, ActionState.DONE),
        (ActionState.STARTED, ActionState.FAILED),
        (ActionState.SHADOW, ActionState.SHADOW),
    }
    if legal:
        database.transition(KEY, after, 13, result)
        updated = database.action(KEY)
        assert updated is not None
        assert updated.finished_time == (13 if result is not None else None)
        assert database.action(KEY) == Action(
            KEY,
            after,
            10,
            13
            if after == ActionState.STARTED
            else (None if after == ActionState.SHADOW else 11),
            13 if result is not None else None,
            result,
        )
    else:
        with pytest.raises(TransitionError) as error:
            database.transition(KEY, after, 13, result)
        assert error.value.code == ErrorCode.TRANSITION
        assert str(error.value) == "transition"
        assert database.action(KEY) == previous


@pytest.mark.parametrize("state", list(ActionState))
@pytest.mark.parametrize("shadow", [False, True])
def test_existing_keys_preserve_live_actions_and_promote_shadows(
    database: Store, state: ActionState, shadow: bool
) -> None:
    advance(database, state)
    previous = database.action(KEY)
    promoted = state == ActionState.SHADOW and not shadow
    assert database.record_intent(KEY, 99, shadow=shadow) == promoted
    assert database.action(KEY) == (
        Action(KEY, ActionState.INTENT, 99, None, None, None) if promoted else previous
    )
    assert database.existing_keys() == (
        frozenset() if state == ActionState.SHADOW and shadow else frozenset({KEY})
    )


def test_missing_action_and_crash_recovery(database: Store) -> None:
    assert database.action(KEY) is None
    assert database.crashed_intents() == ()
    assert database.existing_keys() == frozenset()
    with pytest.raises(TransitionError):
        database.transition(KEY, ActionState.STARTED, 0)
    for kind in ("review", "merge", "approve"):
        assert database.record_intent(f"{kind}:{BASE}@{HEAD}", 0)
    assert database.record_intent("sweep:1", 0, shadow=True)
    assert database.crashed_intents() == (
        f"approve:{BASE}@{HEAD}",
        f"merge:{BASE}@{HEAD}",
        KEY,
    )
    database.transition(KEY, ActionState.STARTED, 0)
    database.transition(f"merge:{BASE}@{HEAD}", ActionState.STARTED, 1)
    database.transition(f"merge:{BASE}@{HEAD}", ActionState.FAILED, 2, Result.ERROR)
    assert database.crashed_intents() == (f"approve:{BASE}@{HEAD}",)
    assert database.action(KEY) == Action(KEY, ActionState.STARTED, 0, 0, None, None)


@pytest.mark.parametrize(
    ("initial", "target", "time", "result"),
    [
        (ActionState.INTENT, ActionState.STARTED, 9, None),
        (ActionState.STARTED, ActionState.DONE, 10, Result.OK),
        (ActionState.INTENT, ActionState.STARTED, 11, Result.OK),
        (ActionState.STARTED, ActionState.DONE, 12, None),
        (ActionState.STARTED, ActionState.FAILED, 12, None),
    ],
)
def test_transition_rejects_reversed_time_and_wrong_result(
    database: Store,
    initial: ActionState,
    target: ActionState,
    time: int,
    result: Result | None,
) -> None:
    advance(database, initial)
    previous = database.action(KEY)
    with pytest.raises(TransitionError):
        database.transition(KEY, target, time, result)
    assert database.action(KEY) == previous


def test_receipt_duplicate_does_not_replace_first(database: Store) -> None:
    assert database.record_receipt(RECEIPT) == DeliveryStatus.RECORDED
    assert (
        database.record_receipt(replace(RECEIPT, time=99)) == DeliveryStatus.DUPLICATE
    )
    assert (
        database.record_receipt(replace(RECEIPT, delivery_id="example-second"))
        == DeliveryStatus.RECORDED
    )


@pytest.mark.parametrize(
    "result",
    [
        Result.OK,
        Result.ERROR,
        Result.TIMEOUT,
        Result.REFUSED,
        Result.AGREE,
        Result.DISAGREE,
        Result.NO_CHANGE,
    ],
)
def test_fixed_results_survive_reopen(tmp_path: Path, result: Result) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    assert database.recovery == Recovery.NEW
    assert database.record_intent(KEY, 10)
    database.transition(KEY, ActionState.STARTED, 10)
    target = {
        Result.OK: ActionState.DONE,
        Result.ERROR: ActionState.FAILED,
        Result.TIMEOUT: ActionState.FAILED,
        Result.REFUSED: ActionState.FAILED,
    }.get(result, ActionState.SHADOW)
    if target == ActionState.SHADOW:
        # Scoring is supplied by the runner; the store only persists the result.
        database.close()
        database = Store(path, mode=OpenMode.REBUILD)
        assert database.record_intent(KEY, 10, shadow=True)
    database.transition(KEY, target, 10, result)
    database.close()
    reopened = Store(path)
    assert reopened.recovery == Recovery.EXISTING
    assert reopened.action(KEY) == Action(
        KEY, target, 10, None if target == ActionState.SHADOW else 10, 10, result
    )
    reopened.close()
    reopened.close()
    with pytest.raises(StoreError) as error:
        reopened.existing_keys()
    assert error.value.code == ErrorCode.CLOSED


def test_all_tables_persist_with_owner_permissions(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    assert path.stat().st_mode & 0o777 == 0o600
    assert database.record_receipt(RECEIPT) == DeliveryStatus.RECORDED
    assert database.record_intent(KEY, 10)
    database.record_run(RUN)
    escalation = f"escalate:{BASE}@{HEAD}:age"
    assert database.record_intent(escalation, 11)
    marker, created = database.record_escalation(escalation, 12)
    assert created
    assert marker == f"<!-- loop:{escalation} -->"
    assert database.record_escalation(escalation, 13) == (marker, False)
    for suffix in ("-wal", "-shm"):
        assert Path(f"{path}{suffix}").stat().st_mode & 0o777 == 0o600
    database.close()
    path.chmod(0o644)
    reopened = Store(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert reopened.record_receipt(RECEIPT) == DeliveryStatus.DUPLICATE
    assert reopened.existing_keys() == frozenset({KEY, escalation})
    assert reopened.counts(BASE) == Counts((), 1, 0)
    reopened.close()
    with store.sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert raw.execute("SELECT * FROM receipts").fetchall() == [
            (
                "example-delivery",
                12,
                "check_run",
                "example-org/example-repo",
                "completed",
                "example-user",
                "[7, 9]",
                HEAD,
                "example check",
                "success",
                None,
            )
        ]
        assert raw.execute("SELECT * FROM runs").fetchall() == [
            (
                RUN.name,
                RUN.host,
                RUN.harness,
                RUN.model,
                KEY,
                "live",
            )
        ]
        assert raw.execute("SELECT * FROM escalations").fetchall() == [
            (escalation, marker, 12)
        ]
    raw.close()


@pytest.mark.parametrize("damaged", [False, True])
def test_lost_or_corrupt_store_requires_explicit_rebuild(
    tmp_path: Path, damaged: bool
) -> None:
    path = tmp_path / "example.sqlite3"
    if damaged:
        path.write_bytes(b"synthetic damaged store")
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == (ErrorCode.CORRUPT if damaged else ErrorCode.MISSING)
    assert str(error.value) == ("corrupt" if damaged else "missing")
    assert path.exists() == damaged
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").write_bytes(b"synthetic sidecar")
    rebuilt = Store(path, mode=OpenMode.REBUILD)
    assert rebuilt.recovery == Recovery.KEYS_LOST
    assert rebuilt.existing_keys() == frozenset()
    assert rebuilt.record_intent(KEY, 0)
    rebuilt.close()


def test_create_does_not_overwrite_and_rebuild_discards_keys(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    assert database.record_intent(KEY, 1)
    database.close()
    with pytest.raises(StoreError) as error:
        Store(path, mode=OpenMode.CREATE)
    assert error.value.code == ErrorCode.EXISTS
    reopened = Store(path)
    assert reopened.existing_keys() == frozenset({KEY})
    reopened.close()
    rebuilt = Store(path, mode=OpenMode.REBUILD)
    assert rebuilt.recovery == Recovery.KEYS_LOST
    assert rebuilt.existing_keys() == frozenset()
    rebuilt.close()


def test_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "example-target"
    target.write_text("synthetic target")
    path = tmp_path / "example-link"
    path.symlink_to(target)
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == ErrorCode.IO
    assert target.read_text() == "synthetic target"


def test_wrong_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    with store.sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE unrelated (value INTEGER)")
    raw.close()
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == ErrorCode.CORRUPT


def test_integrity_check_rejects_damaged_rows(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    with store.sqlite3.connect(path) as raw:
        raw.executescript(
            "CREATE TABLE example (value INTEGER CHECK(value > 0));"
            "PRAGMA ignore_check_constraints = ON;"
            "INSERT INTO example VALUES (-1);"
        )
    raw.close()
    with pytest.raises(StoreError) as error:
        Store(path)
    assert error.value.code == ErrorCode.CORRUPT


def test_connection_failure_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> store.sqlite3.Connection:
        message = "synthetic failure"
        raise store.sqlite3.DatabaseError(message)

    monkeypatch.setattr(store.sqlite3, "connect", fail)
    with pytest.raises(StoreError) as error:
        Store(":memory:")
    assert error.value.code == ErrorCode.CORRUPT
    assert str(error.value) == "corrupt"


@pytest.mark.parametrize("column", ["state", "result"])
def test_invalid_stored_codes_fail_closed(tmp_path: Path, column: str) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    assert database.record_intent(KEY, 0)
    with store.sqlite3.connect(path) as raw:
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute(f"UPDATE actions SET {column} = 'untrusted'")  # noqa: S608 -- fixed columns
    raw.close()
    with pytest.raises(StoreError) as error:
        database.action(KEY)
    assert error.value.code == ErrorCode.CORRUPT
    with pytest.raises(StoreError) as error:
        database.existing_keys()
    assert error.value.code == ErrorCode.CLOSED


def test_schema_disappearing_during_operation_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = Store(path, mode=OpenMode.CREATE)
    with store.sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE actions")
    raw.close()
    with pytest.raises(StoreError) as error:
        database.existing_keys()
    assert error.value.code == ErrorCode.IO
    with pytest.raises(StoreError) as error:
        database.existing_keys()
    assert error.value.code == ErrorCode.CLOSED


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome.LIVE,
        Outcome.APPROVE,
        Outcome.COMMENT,
        Outcome.CANNOT_REVIEW,
        Outcome.FAILED,
        Outcome.STOPPED,
        Outcome.UNKNOWN,
    ],
)
def test_run_observations_update_in_place(database: Store, outcome: Outcome) -> None:
    assert database.record_intent(KEY, 1)
    database.record_run(RUN)
    database.record_run(replace(RUN, outcome=outcome))
    assert database.counts(BASE) == Counts((), 1, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"host": "example-other.example.com"},
        {"harness": "claude"},
        {"model": "example-other"},
        {"key": f"merge:{BASE}@{HEAD}"},
    ],
)
def test_named_run_cannot_change_identity(
    database: Store, change: dict[str, object]
) -> None:
    assert database.record_intent(KEY, 0)
    database.record_run(RUN)
    with pytest.raises(StoreError) as error:
        database.record_run(changed_run(change))
    assert error.value.code == ErrorCode.INVALID
    assert database.counts(BASE) == Counts((), 1, 0)


def test_only_one_run_per_action_and_foreign_keys(database: Store) -> None:
    with pytest.raises(StoreError) as error:
        database.record_run(RUN)
    assert error.value.code == ErrorCode.INVALID
    assert database.record_intent(KEY, 0)
    database.record_run(RUN)
    with pytest.raises(StoreError) as error:
        database.record_run(replace(RUN, name="example-second"))
    assert error.value.code == ErrorCode.INVALID
    escalation = "escalate:example-run:retry_failed"
    with pytest.raises(StoreError) as error:
        database.record_escalation(escalation, 0)
    assert error.value.code == ErrorCode.INVALID
    assert database.record_intent(escalation, 0)
    assert database.record_escalation(escalation, 1) == (
        f"<!-- loop:{escalation} -->",
        True,
    )


def test_counts_span_heads_but_keep_families_and_objects_separate(
    database: Store,
) -> None:
    assert database.counts(BASE) == Counts((), 0, 0)
    for head in ("a" * 40, "b" * 40, "c" * 40):
        key = f"review:{BASE}@{head}"
        assert database.record_intent(key, 0)
        database.transition(key, ActionState.STARTED, 1)
        database.record_run(replace(RUN, name=f"example-run-{head}", key=key))
        database.transition(key, ActionState.FAILED, 2, Result.TIMEOUT)
    merge = f"merge:{BASE}@{HEAD}"
    assert database.record_intent(merge, 0)
    database.transition(merge, ActionState.STARTED, 1)
    database.transition(merge, ActionState.FAILED, 2, Result.REFUSED)
    other = f"review:example-org/example-repo#70@{HEAD}"
    assert database.record_intent(other, 0)
    database.transition(other, ActionState.STARTED, 1)
    database.transition(other, ActionState.FAILED, 2, Result.ERROR)
    counts = database.counts(BASE)
    assert counts == Counts(
        (FamilyCount(f"merge:{BASE}", 1), FamilyCount(f"review:{BASE}", 3)), 3, 0
    )
    assert counts.action_failures == (1, 3)
    assert counts.reviewer_runs == 3
    run = rules.Run(
        "example-run", "codex", "unknown", 0, 10, action_failures=counts.action_failures
    )
    assert run.action_failures == (1, 3)
    assert database.counts("example-org/example-repo#70").action_failures == (1,)


def test_shadow_done_and_pending_never_count_as_failures(database: Store) -> None:
    states = (
        ActionState.SHADOW,
        ActionState.INTENT,
        ActionState.STARTED,
        ActionState.DONE,
        ActionState.FAILED,
    )
    for index, state in enumerate(states):
        key = f"review:{BASE}@{index:040x}"
        assert database.record_intent(key, 0, shadow=state == ActionState.SHADOW)
        database.record_run(replace(RUN, name=f"example-run-{index}", key=key))
        if state in {ActionState.STARTED, ActionState.DONE, ActionState.FAILED}:
            database.transition(key, ActionState.STARTED, 1)
        if state in {ActionState.DONE, ActionState.FAILED}:
            database.transition(
                key,
                state,
                2,
                Result.ERROR if state == ActionState.FAILED else Result.OK,
            )
    assert database.counts(BASE) == Counts((FamilyCount(f"review:{BASE}", 1),), 4, 0)
    assert database.counts(BASE).action_failures == (1,)


def test_retry_counts_and_failed_run_action_family(database: Store) -> None:
    retry = "retry:example-run"
    assert database.record_intent(retry, 0)
    database.record_run(replace(RUN, name="example-retry", key=retry, harness="claude"))
    assert database.counts("example-run") == Counts((), 0, 1)
    database.transition(retry, ActionState.STARTED, 1)
    database.transition(retry, ActionState.FAILED, 2, Result.TIMEOUT)
    assert database.counts("example-run") == Counts((FamilyCount(retry, 1),), 0, 1)
    assert database.counts("example-other") == Counts((), 0, 0)


@pytest.mark.parametrize(
    "value", ["", "untrusted\ntext", "review:bad", "sweep:-1", "sweep:" + "1" * 20]
)
def test_bad_action_keys_are_rejected(database: Store, value: str) -> None:
    with pytest.raises(StoreError) as error:
        database.record_intent(value, 0)
    assert error.value.code == ErrorCode.INVALID
    assert str(error.value) == "invalid"


@pytest.mark.parametrize("value", [-1, 2**63, True, 1.5, "1"])
def test_bad_times_are_rejected(database: Store, value: object) -> None:
    with pytest.raises(StoreError) as error:
        database.record_intent(KEY, cast("int", value))
    assert error.value.code == ErrorCode.INVALID


@pytest.mark.parametrize("time", [0, 2**63 - 1])
def test_time_boundaries_are_storable(database: Store, time: int) -> None:
    assert database.record_intent(KEY, time)
    assert database.action(KEY) == Action(
        KEY, ActionState.INTENT, time, None, None, None
    )


def test_creation_is_private_before_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = store.os.fchmod
    original_open = store.os.open

    def open_private(path: Path, flags: int, mode: int = 0o777) -> int:
        assert mode == 0o600
        assert flags & store.os.O_RDWR == store.os.O_RDWR
        assert flags & store.os.O_NOFOLLOW
        assert flags & store.os.O_NONBLOCK
        return original_open(path, flags, mode)

    def chmod(descriptor: int, mode: int) -> None:
        assert store.os.fstat(descriptor).st_mode & 0o777 == 0o600
        original(descriptor, mode)

    monkeypatch.setattr(store.os, "fchmod", chmod)
    monkeypatch.setattr(store.os, "open", open_private)
    database = Store(tmp_path / "example.sqlite3", mode=OpenMode.CREATE)
    database.close()


def test_connection_settings_and_single_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[store.sqlite3.Connection] = []
    original = store.sqlite3.connect

    def connect(path: Path | Literal[":memory:"]) -> store.sqlite3.Connection:
        connection = original(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(store.sqlite3, "connect", connect)
    database = Store(tmp_path / "example.sqlite3", mode=OpenMode.CREATE)
    assert len(connections) == 1
    connection = connections[0]
    assert connection.execute("PRAGMA busy_timeout").fetchone() == (5000,)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    database.close()


@pytest.mark.parametrize(
    "change",
    [
        {"delivery_id": "untrusted\ntext"},
        {"delivery_id": 7},
        {"time": -1},
        {"event": replace(EVENT, event="untrusted")},
        {"event": replace(EVENT, action="untrusted")},
        {"event": replace(EVENT, repository="untrusted")},
        {"event": replace(EVENT, sender_login="untrusted\ntext")},
        {"event": replace(EVENT, head_sha="a" * 39)},
        {
            "event": replace(
                EVENT, pr_numbers=cast("tuple[int, ...]", cast("object", []))
            )
        },
        {"event": replace(EVENT, pr_numbers=(0,))},
        {"event": replace(EVENT, pr_numbers=(-1,))},
        {"event": replace(EVENT, pr_numbers=(True,))},
        {"event": replace(EVENT, pr_numbers=(2**31,))},
        {"event": replace(EVENT, check_name="untrusted\ntext")},
        {"event": replace(EVENT, conclusion="untrusted")},
        {"event": replace(EVENT, review_state="untrusted")},
    ],
)
def test_receipt_rejects_unvalidated_fields(change: dict[str, object]) -> None:
    with pytest.raises(StoreError) as error:
        Receipt(
            cast("str", change.get("delivery_id", RECEIPT.delivery_id)),
            cast("int", change.get("time", RECEIPT.time)),
            cast("events.Event", change.get("event", RECEIPT.event)),
        )
    assert error.value.code == ErrorCode.INVALID


def test_receipt_accepts_optional_fields_and_number_boundaries() -> None:
    for number in (1, 2**31 - 1):
        receipt = replace(
            RECEIPT, event=replace(EVENT, pr_numbers=(number,), review_state="approved")
        )
        assert receipt.event.pr_numbers == (number,)
    assert (
        replace(
            RECEIPT,
            event=replace(EVENT, pr_numbers=(), check_name=None, conclusion=None),
        ).event.pr_numbers
        == ()
    )


@pytest.mark.parametrize(
    "change",
    [
        {"name": "untrusted\ntext"},
        {"host": "untrusted\ntext"},
        {"model": "untrusted\ntext"},
        {"key": "untrusted"},
        {"harness": "untrusted"},
        {"outcome": "untrusted"},
    ],
)
def test_run_rejects_unvalidated_fields(change: dict[str, object]) -> None:
    with pytest.raises(StoreError) as error:
        changed_run(change)
    assert error.value.code == ErrorCode.INVALID


def test_enum_inputs_are_closed(database: Store) -> None:
    with pytest.raises(StoreError) as error:
        Store(":memory:", mode=cast("OpenMode", "open"))
    assert error.value.code == ErrorCode.INVALID
    with pytest.raises(StoreError):
        database.record_intent(KEY, 0, shadow=cast("bool", 1))
    with pytest.raises(StoreError):
        database.transition(KEY, cast("ActionState", "untrusted"), 0)
    with pytest.raises(StoreError):
        database.transition(KEY, ActionState.DONE, 0, cast("Result", "untrusted"))
    with pytest.raises(StoreError):
        database.counts("untrusted\ntext")
    with pytest.raises(StoreError):
        database.record_escalation(KEY, 0)


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (RECEIPT, "time"),
        (RUN, "outcome"),
        (Action(KEY, ActionState.INTENT, 0, None, None, None), "state"),
        (Counts((), 0, 0), "retries"),
        (FamilyCount(f"review:{BASE}", 1), "failures"),
    ],
)
def test_records_are_frozen(value: object, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(value, field, None)


@pytest.mark.parametrize(
    "key",
    [
        "sweep:0",
        "sweep:9999999999999999999",
        f"review:{BASE}@{HEAD}",
        f"approve:{BASE}@{HEAD}",
        f"merge:{BASE}@{HEAD}",
        f"rerun:{BASE}@{HEAD}",
        f"verify:{BASE}",
        f"follow:{BASE}:example-step",
        f"notify:{BASE}@{HEAD}:ci_red",
        "collect:example-run",
        "retry:example-run",
        "stop:example-run",
        f"escalate:{BASE}@{HEAD}:action_failed",
        "escalate:example-run:retry_failed",
    ],
)
def test_all_rule_key_shapes_are_accepted(database: Store, key: str) -> None:
    assert database.record_intent(key, 0)
    assert database.action(key) == Action(key, ActionState.INTENT, 0, None, None, None)
