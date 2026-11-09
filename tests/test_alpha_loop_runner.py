"""Synthetic event sequences and read snapshots; all effects are recording fakes."""

# cspell:words unreviewed

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from test_alpha_loop_rules import (
    APPROVED,
    ARGS,
    BUDGET,
    DECLINED,
    HEAD,
    MERGED,
    PR,
    REPO,
    RUN,
    SWEEP,
)
from test_alpha_loop_store import EVENT

from tbhbdev.alpha.loop import config, rules, runner, scoring, store

if TYPE_CHECKING:
    from collections.abc import Iterable


class Source:
    def __init__(self, *deliveries: runner.Delivery) -> None:
        self.items = deliveries
        self.calls = 0

    def deliveries(self) -> Iterable[runner.Delivery]:
        self.calls += 1
        return self.items


class Reader:
    def __init__(self, snapshot: runner.Snapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, ...]] = []

    def read(self, repositories: tuple[str, ...]) -> runner.Snapshot:
        self.calls.append(repositories)
        return self.snapshot


class Writer:
    def __init__(self) -> None:
        self.calls: list[rules.Intent] = []

    def execute(self, intent: rules.Intent) -> None:
        self.calls.append(intent)


class Approver:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], rules.PRArguments]] = []

    def execute(self, argv: tuple[str, ...], arguments: rules.PRArguments) -> None:
        self.calls.append((argv, arguments))


def settings() -> config.LoopConfig:
    return config.LoopConfig(
        (REPO,),
        ("example-approve",),
        Path("/example/state/loop.sqlite3"),
        15,
        3,
        1,
        40,
        tuple(host.name for host in BUDGET.hosts),
        rule_modes=tuple(
            config.RuleMode(REPO, cast("rules.Rule", f"R{i}"), "shadow")
            for i in range(12)
        ),
    )


def make_runner(
    database: store.Store, loaded: config.LoopConfig | None = None
) -> runner.Runner:
    return runner.Runner(
        loaded or settings(),
        database,
        io.StringIO(),
        runner.Effects(Writer(), Writer(), Approver()),
    )


def delivery(
    pr: rules.PullRequest = PR, name: str = "example-delivery", time: int = 10
) -> runner.Delivery:
    return runner.Delivery(
        store.Receipt(
            name, time, replace(EVENT, pr_numbers=(pr.number,), head_sha=pr.head_sha)
        ),
        runner.Snapshot(BUDGET, (pr,)),
    )


def lines(subject: runner.Runner) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in cast("io.StringIO", subject.log).getvalue().splitlines()
    ]


def test_full_scenario_never_calls_effects(tmp_path: Path) -> None:
    path = tmp_path / "example.sqlite3"
    database = store.Store(path, mode=store.OpenMode.CREATE)
    subject = make_runner(database)
    read = Reader(runner.Snapshot(BUDGET, ()))
    source = Source(delivery(), delivery(), delivery(APPROVED, "example-approved", 11))
    subject.run(source, read, replace(SWEEP, trigger="startup"), 1)
    assert read.calls == [(REPO,)]
    assert source.calls == 1
    assert lines(subject) == [
        {
            "source": "sweep:12",
            "state": "reconcile",
            "rule": "R0",
            "action_key": "sweep:12",
            "action_kind": "read",
            "outcome": "shadow",
        },
        {
            "source": "example-delivery",
            "state": "green_unreviewed",
            "rule": "R1",
            "action_key": f"review:{REPO}#7@{HEAD}",
            "action_kind": "spawn",
            "outcome": "shadow",
        },
        {
            "source": "example-approved",
            "state": "approved",
            "rule": "R4",
            "action_key": f"merge:{REPO}#7@{HEAD}",
            "action_kind": "fixed",
            "outcome": "shadow",
        },
    ]
    database.close()
    # Restart: no stored PR state; a fresh read exposes approval with a stalled merge.
    database = store.Store(path)
    restarted = make_runner(database)
    read.snapshot = runner.Snapshot(BUDGET, (replace(APPROVED, state_age=901),))
    restarted.run(Source(), read, replace(SWEEP, time_slot=13, trigger="startup"), 1000)
    assert read.calls == [(REPO,), (REPO,)]
    assert [line["rule"] for line in lines(restarted)] == ["R0", "R9"]
    report = restarted.report()
    assert report.r9_items == 1
    assert next(c for c in report.counts if c.rule == "R1").agree == 1
    assert next(c for c in report.counts if c.rule == "R4").no_change == 1
    read.snapshot = runner.Snapshot(BUDGET, (MERGED,))
    restarted.reconcile(read, replace(SWEEP, time_slot=14), 2000)
    assert next(c for c in restarted.report().counts if c.rule == "R4").agree == 1
    assert restarted.report().r9_items == 1
    for instance in (subject, restarted):
        assert cast("Writer", instance.effects.peer).calls == []
        assert cast("Writer", instance.effects.github).calls == []
        assert cast("Approver", instance.effects.approval).calls == []
    database.close()


def test_off_modes_and_unimplemented_live_mode() -> None:
    database = store.Store(":memory:")
    subject = make_runner(database, replace(settings(), rule_modes=()))
    subject.intake(delivery())
    reader = Reader(runner.Snapshot(BUDGET))
    subject.reconcile(reader, SWEEP, 11)
    assert reader.calls == []
    assert lines(subject) == []
    assert subject.report() == scoring.Summary((), 0)
    with pytest.raises(runner.LiveModeError, match=r"^live_intake_not_wired$"):
        make_runner(
            database,
            replace(settings(), rule_modes=(config.RuleMode(REPO, "R11", "on"),)),
        )
    database.close()


def test_budget_guard_prevents_reconcile_read() -> None:
    database = store.Store(":memory:")
    subject = make_runner(database)
    reader = Reader(runner.Snapshot(BUDGET))
    subject.reconcile(reader, replace(SWEEP, points_remaining=0), 1)
    assert reader.calls == []
    assert lines(subject) == []
    database.close()


def test_decline_and_run_intents_use_typed_context() -> None:
    database = store.Store(":memory:")
    subject = make_runner(database)
    subject.intake(delivery(DECLINED))
    assert [(item["rule"], item["state"]) for item in lines(subject)] == [
        ("R3", "review_declined"),
        ("R10", "review_declined"),
    ]
    reader = Reader(
        runner.Snapshot(
            BUDGET, runs=(runner.ObservedRun(REPO, replace(RUN, state="finished")),)
        )
    )
    subject.reconcile(reader, SWEEP, 11)
    assert lines(subject)[-1] == {
        "source": "sweep:12",
        "state": "finished",
        "rule": "R7",
        "action_key": "collect:example-run",
        "action_kind": "fixed",
        "outcome": "shadow",
    }
    reader.snapshot = replace(
        reader.snapshot, runs=(runner.ObservedRun(REPO, replace(RUN, state="unknown")),)
    )
    subject.reconcile(reader, replace(SWEEP, time_slot=13), 12)
    assert lines(subject)[-1]["rule"] == "R8"
    database.close()


@pytest.mark.parametrize(
    "change", ["repository", "number", "head", "runs", "unwatched"]
)
def test_delivery_context_must_match(change: str) -> None:
    database = store.Store(":memory:")
    subject = make_runner(database)
    item = delivery()
    if change == "repository":
        item = replace(
            item,
            snapshot=runner.Snapshot(
                BUDGET, (replace(PR, repository="example-org/other"),)
            ),
        )
    elif change == "number":
        item = replace(item, snapshot=runner.Snapshot(BUDGET, (replace(PR, number=8),)))
    elif change == "head":
        item = replace(
            item, snapshot=runner.Snapshot(BUDGET, (replace(PR, head_sha="b" * 40),))
        )
    elif change == "runs":
        item = replace(
            item,
            snapshot=runner.Snapshot(BUDGET, runs=(runner.ObservedRun(REPO, RUN),)),
        )
    else:
        item = replace(
            item,
            receipt=replace(
                item.receipt, event=replace(EVENT, repository="example-org/other")
            ),
        )
    with pytest.raises(runner.ObservationError, match=r"^invalid_observation$"):
        subject.intake(item)
    assert subject.report().counts == ()
    database.close()


def test_fake_methods_record_if_called() -> None:
    writer = Writer()
    intent = rules.Intent("R4", "fixed", f"merge:{REPO}#7@{HEAD}", ARGS)
    writer.execute(intent)
    assert writer.calls == [intent]
    approval = Approver()
    approval.execute(("example-approve",), ARGS)
    assert approval.calls == [(("example-approve",), ARGS)]


@pytest.mark.parametrize(("count", "expected"), [(19, False), (20, True), (21, True)])
def test_eligibility_boundary(count: int, expected: bool) -> None:
    assert scoring.eligible(count) is expected
    assert (
        scoring.RuleCounts(REPO, "R2", count, count - 1, 1, 99, 99).eligible is expected
    )


def test_duplicate_sweep_logs_only_once_and_still_reads() -> None:
    db = store.Store(":memory:")
    subject = make_runner(db)
    reader = Reader(runner.Snapshot(BUDGET, (PR,)))
    subject.reconcile(reader, SWEEP, 1)
    subject.reconcile(reader, SWEEP, 2)
    assert reader.calls == [(REPO,), (REPO,)]
    assert len(lines(subject)) == 2
    db.close()


def test_reconcile_dimensions_cover_each_repository_and_skip_unread_scores() -> None:
    other = "example-org/other"
    db = store.Store(":memory:")
    loaded = replace(
        settings(),
        repositories=(REPO, other),
        rule_modes=(
            *settings().rule_modes,
            config.RuleMode(other, "R0", "shadow"),
        ),
    )
    subject = make_runner(db, loaded)
    reader = Reader(runner.Snapshot(BUDGET))
    subject.reconcile(reader, SWEEP, 1)
    assert reader.calls == [(REPO, other)]
    assert len(lines(subject)) == 1
    assert [(c.repository, c.rule) for c in subject.report().counts] == [
        (REPO, "R0"),
        (other, "R0"),
    ]
    assert db.record_shadow(
        store.ShadowIntent(f"merge:{other}#1@{HEAD}", other, "R4"), 1
    )
    reduced = make_runner(db)
    reduced.reconcile(reader, replace(SWEEP, time_slot=13), 2)
    action = db.action(f"merge:{other}#1@{HEAD}")
    assert action is not None
    assert action.result is None
    db.close()


def test_live_counts_come_from_store_without_stored_state() -> None:
    db = store.Store(":memory:")
    subject = make_runner(db)
    for i in range(3):
        key = f"review:{REPO}#7@{i:040x}"
        assert db.record_intent(key, 1)
        db.record_run(
            store.RunRecord(
                f"example-run-{i}",
                "example-host",
                "codex",
                "example-model",
                key,
                store.Outcome.FAILED,
            )
        )
        db.transition(key, store.ActionState.STARTED, 2)
        db.transition(key, store.ActionState.FAILED, 3, store.Result.ERROR)
    subject.intake(delivery())
    assert lines(subject)[-1]["rule"] == "R9"
    snapshot = runner.Snapshot(BUDGET, (APPROVED,))
    assert (
        subject._inputs(snapshot).pull_requests[0].reviewer_runs_without_approval == 0  # noqa: SLF001 -- assert the rules boundary
    )
    retry = "retry:example-run"
    assert db.record_intent(retry, 1)
    db.record_run(
        store.RunRecord(
            "example-retry",
            "example-host",
            "codex",
            "example-model",
            retry,
            store.Outcome.UNKNOWN,
        )
    )
    reader = Reader(
        runner.Snapshot(
            BUDGET, runs=(runner.ObservedRun(REPO, replace(RUN, state="unknown")),)
        )
    )
    subject.reconcile(reader, SWEEP, 11)
    assert lines(subject)[-1]["action_key"] == "escalate:example-run:retry_failed"
    db.close()


def test_off_non_spawn_decisions_remain_unrecorded() -> None:
    db = store.Store(":memory:")
    subject = make_runner(db, replace(settings(), rule_modes=()))
    subject.intake(delivery(DECLINED))
    assert lines(subject) == []
    assert db.shadow_keys() == frozenset()
    db.close()


@pytest.mark.parametrize("off", [False, True])
def test_startup_requires_reconcile_before_consuming_deliveries(off: bool) -> None:
    db = store.Store(":memory:")
    loaded = replace(settings(), rule_modes=()) if off else settings()
    subject = make_runner(db, loaded)
    source = Source(delivery())
    with pytest.raises(
        runner.ReconcileRequiredError, match=r"^startup_reconcile_required$"
    ):
        subject.run(
            source,
            Reader(runner.Snapshot(BUDGET)),
            replace(SWEEP, points_remaining=0),
            1,
        )
    assert source.calls == 0
    db.close()


def test_configured_interval_controls_sweep_schedule() -> None:
    db = store.Store(":memory:")
    subject = make_runner(db, replace(settings(), reconcile_interval_minutes=20))
    reader = Reader(runner.Snapshot(BUDGET))
    assert subject.reconcile(reader, SWEEP, 1) is False
    assert reader.calls == []
    assert subject.reconcile(reader, replace(SWEEP, age=1200), 2) is True
    assert reader.calls == [(REPO,)]
    db.close()


@pytest.mark.parametrize("approved", [False, True])
def test_inputs_project_live_counts_config_and_keys(approved: bool) -> None:
    db = store.Store(":memory:")
    loaded = replace(
        settings(),
        hosts=(BUDGET.hosts[0].name,),
        allow_claude_retry=True,
        rule_modes=(
            config.RuleMode(REPO, "R0", "shadow"),
            config.RuleMode(REPO, "R8", "shadow"),
        ),
    )
    subject = make_runner(db, loaded)
    review = f"review:{REPO}#7@{'b' * 40}"
    retry = "retry:example-run"
    stopped = "stop:example-stopped"
    shadow = f"approve:{REPO}#7@{HEAD}"
    for key, name in ((review, "example-review"), (retry, "example-retry")):
        assert db.record_intent(key, 1)
        db.record_run(
            store.RunRecord(
                name,
                "example-host",
                "codex",
                "example-model",
                key,
                store.Outcome.FAILED,
            )
        )
        db.transition(key, store.ActionState.STARTED, 2)
        db.transition(key, store.ActionState.FAILED, 3, store.Result.ERROR)
    assert db.record_intent(stopped, 1)
    assert db.record_intent(shadow, 1, shadow=True)
    pr = replace(
        APPROVED if approved else PR,
        reviewer_runs_without_approval=99,
        action_failures=(99,),
    )
    run = replace(RUN, retries=99, action_failures=(99,))
    snapshot = runner.Snapshot(
        replace(BUDGET, total_cap=99, per_host_cap=99, daily_cap=99),
        (pr,),
        (runner.ObservedRun(REPO, run),),
    )
    disabled: set[tuple[str, rules.Rule]] = {
        (REPO, rule) for rule in rules.RULES if rule not in {"R0", "R8"}
    }
    disabled.add((RUN.name, "R9"))
    expected = rules.Inputs(
        replace(BUDGET, hosts=(BUDGET.hosts[0],)),
        (
            replace(
                pr,
                reviewer_runs_without_approval=0 if approved else 1,
                action_failures=(1,),
            ),
        ),
        (replace(run, retries=1, action_failures=(1,)),),
        existing_keys=frozenset({review, retry, stopped, shadow}),
        stopped_objects=frozenset({"example-stopped"}),
        allow_claude_retry=True,
        disabled_rules=frozenset(disabled),
    )
    assert subject._inputs(snapshot) == expected  # noqa: SLF001 -- rules boundary
    db.close()


@pytest.mark.parametrize("run", [False, True])
def test_reconcile_rejects_unwatched_facts(run: bool) -> None:
    db = store.Store(":memory:")
    subject = make_runner(db)
    other = "example-org/other"
    snapshot = (
        runner.Snapshot(BUDGET, runs=(runner.ObservedRun(other, RUN),))
        if run
        else runner.Snapshot(BUDGET, (replace(PR, repository=other),))
    )
    with pytest.raises(runner.ObservationError):
        subject.reconcile(Reader(snapshot), SWEEP, 1)
    assert db.shadow_intents() == ()
    db.close()


def test_mixed_modes_are_scoped_to_repository_and_rule() -> None:
    db = store.Store(":memory:")
    loaded = replace(
        settings(),
        rule_modes=(
            config.RuleMode(REPO, "R0", "shadow"),
            config.RuleMode(REPO, "R1", "off"),
        ),
    )
    subject = make_runner(db, loaded)
    subject.intake(delivery())
    assert lines(subject) == []
    db.close()


def test_delivery_identity_cannot_generate_new_keys_on_replay() -> None:
    db = store.Store(":memory:")
    subject = make_runner(db)
    first = delivery()
    subject.intake(first)
    replay = delivery(replace(APPROVED, head_sha="b" * 40), time=11)
    subject.intake(replay)
    assert db.shadow_keys() == frozenset({f"review:{REPO}#7@{HEAD}"})
    assert len(lines(subject)) == 1
    db.close()
