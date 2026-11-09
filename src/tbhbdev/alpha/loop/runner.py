"""Shadow orchestration over typed intake and reconcile interfaces.

No adapters exist here. Every configured ``on`` mode raises LiveModeError at
startup, including rules on repositories without observations. Effects are
accepted as interfaces to make the zero-write contract observable in tests;
shadow execution never invokes them. Event snapshots are derived by intake
from the delivered event and verified context, without confirmation reads.
A restart always reconciles before processing deliveries; no PR state is stored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, cast

from tbhbdev.alpha.loop import config, rules, state, store
from tbhbdev.alpha.loop.scoring import Summary, score, summarize

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import TextIO


@dataclass(frozen=True)
class ObservedRun:
    repository: str
    run: rules.Run


@dataclass(frozen=True)
class Effect:
    """Typed evidence for effects that PR facts alone cannot establish."""

    key: str
    result: store.Result


@dataclass(frozen=True)
class Snapshot:
    budget: rules.SpawnBudget
    pull_requests: tuple[rules.PullRequest, ...] = ()
    runs: tuple[ObservedRun, ...] = ()
    effects: tuple[Effect, ...] = ()


@dataclass(frozen=True)
class Delivery:
    receipt: store.Receipt
    snapshot: Snapshot


class EventSource(Protocol):
    def deliveries(self) -> Iterable[Delivery]: ...


class ReconcileReader(Protocol):
    def read(self, repositories: tuple[str, ...]) -> Snapshot: ...


class PeerAgent(Protocol):
    def execute(self, intent: rules.Intent) -> None: ...


class GitHubWriter(Protocol):
    def execute(self, intent: rules.Intent) -> None: ...


class Approval(Protocol):
    def execute(self, argv: tuple[str, ...], arguments: rules.PRArguments) -> None: ...


@dataclass(frozen=True)
class Effects:
    peer: PeerAgent
    github: GitHubWriter
    approval: Approval


class LiveModeError(NotImplementedError):
    def __init__(self) -> None:
        super().__init__("live_intake_not_wired")


class ReconcileRequiredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("startup_reconcile_required")


class ObservationError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_observation")


READ_BUDGET = rules.SpawnBudget(0, 0, 0, 0, 0, (), ())


class Runner:
    def __init__(
        self,
        settings: config.LoopConfig,
        database: store.Store,
        log: TextIO,
        effects: Effects,
    ) -> None:
        if any(item.mode == "on" for item in settings.rule_modes):
            raise LiveModeError
        self.settings = settings
        self.database = database
        self.log = log
        self.effects = effects

    def _mode(self, repository: str, rule: rules.Rule) -> config.Mode:
        return next(
            (
                item.mode
                for item in self.settings.rule_modes
                if item.repository == repository and item.rule == rule
            ),
            "off",
        )

    def _validate(self, snapshot: Snapshot) -> None:
        repositories = {pr.repository for pr in snapshot.pull_requests} | {
            observed.repository for observed in snapshot.runs
        }
        if not repositories <= set(self.settings.repositories):
            raise ObservationError

    def _inputs(self, snapshot: Snapshot) -> rules.Inputs:
        self._validate(snapshot)
        prs: list[rules.PullRequest] = []
        for pr in snapshot.pull_requests:
            counts = self.database.counts(f"{pr.repository}#{pr.number}")
            prs.append(
                replace(
                    pr,
                    reviewer_runs_without_approval=(
                        0 if pr.facts.approvals_at_head else counts.reviewer_runs
                    ),
                    action_failures=counts.action_failures,
                )
            )
        runs: list[rules.Run] = []
        for observed in snapshot.runs:
            counts = self.database.counts(observed.run.name)
            runs.append(
                replace(
                    observed.run,
                    retries=counts.retries,
                    action_failures=counts.action_failures,
                )
            )
        settings = self.settings
        budget = replace(
            snapshot.budget,
            total_cap=settings.spawn_cap_total,
            per_host_cap=settings.spawn_cap_per_host,
            daily_cap=settings.daily_spawn_cap,
            hosts=tuple(
                host for host in snapshot.budget.hosts if host.name in settings.hosts
            ),
        )
        run_rules: tuple[rules.Rule, ...] = ("R8", "R9")
        return rules.Inputs(
            budget,
            tuple(prs),
            tuple(runs),
            existing_keys=self.database.existing_keys() | self.database.shadow_keys(),
            stopped_objects=self.database.stopped_objects(),
            allow_claude_retry=settings.allow_claude_retry,
            disabled_rules=frozenset(
                (repo, rule)
                for repo in settings.repositories
                for rule in rules.RULES
                if self._mode(repo, rule) == "off"
            )
            | frozenset(
                (observed.run.name, rule)
                for observed in snapshot.runs
                for rule in run_rules
                if self._mode(observed.repository, rule) == "off"
            ),
        )

    def _record(
        self,
        intent: rules.Intent,
        repository: str,
        derived: str,
        source: str,
        time: int,
    ) -> None:
        if self._mode(repository, intent.rule) == "off":
            return
        if intent.key and not self.database.record_shadow(
            store.ShadowIntent(intent.key, repository, intent.rule), time
        ):
            return
        self.log.write(
            json.dumps(
                {
                    "source": source,
                    "state": derived,
                    "rule": intent.rule,
                    "action_key": intent.key,
                    "action_kind": intent.kind,
                    "outcome": "shadow",
                },
            )
            + "\n"
        )

    def _decide(self, snapshot: Snapshot, source: str, time: int) -> None:
        inputs = self._inputs(snapshot)
        contexts: dict[str, tuple[str, str]] = {}
        for pr in snapshot.pull_requests:
            base = f"{pr.repository}#{pr.number}"
            context = (pr.repository, state.derive(pr.facts).state.value)
            contexts[base] = contexts[f"{base}@{pr.head_sha}"] = context
        for observed in snapshot.runs:
            contexts[observed.run.name] = (observed.repository, observed.run.state)
        for intent in rules.decide(inputs):
            if intent.key:
                obj = intent.key.split(":")[1]
            else:
                # R3 is the only decision without an action key.
                args = cast(
                    "rules.PRArguments",  # pragma: no mutate (typing only)
                    intent.arguments,
                )
                obj = f"{args.repository}#{args.number}"
            repository, derived = contexts[obj]
            self._record(intent, repository, derived, source, time)

    def intake(self, delivery: Delivery) -> None:
        """Act on delivery facts without re-reading GitHub, even on replay."""
        receipt = delivery.receipt
        event = receipt.event
        self._validate(delivery.snapshot)
        if (
            event.repository not in self.settings.repositories
            or any(
                (pr.repository != event.repository)
                or (pr.number not in event.pr_numbers)
                or (pr.head_sha != event.head_sha)
                for pr in delivery.snapshot.pull_requests
            )
            or delivery.snapshot.runs
        ):
            raise ObservationError
        # Reconcile recovers partial intake. A redelivery cannot replace the
        # first receipt's identity or generate keys from different observations.
        if self.database.record_receipt(receipt) == store.DeliveryStatus.DUPLICATE:
            return
        self._decide(delivery.snapshot, receipt.delivery_id, receipt.time)

    def reconcile(self, reader: ReconcileReader, sweep: rules.Sweep, time: int) -> bool:
        repositories = tuple(
            repo
            for repo in self.settings.repositories
            if self._mode(repo, "R0") == "shadow"
        )
        if not repositories:
            return False
        decisions = rules.decide(
            rules.Inputs(
                READ_BUDGET,
                sweep=replace(
                    sweep, interval=self.settings.reconcile_interval_minutes * 60
                ),
            )
        )
        if not decisions:
            return False
        intent = decisions[0]
        snapshot = reader.read(repositories)
        self._validate(snapshot)
        for pending in self.database.shadow_intents():
            if pending.repository in repositories:
                self.database.score_shadow(pending.key, time, score(pending, snapshot))
        # R0 dimensions share one key, so only its first claim logs a decision.
        for repository in repositories:
            self._record(intent, repository, "reconcile", intent.key, time)
        self._decide(snapshot, intent.key, time)
        return True

    def run(
        self,
        source: EventSource,
        reader: ReconcileReader,
        sweep: rules.Sweep,
        time: int,
    ) -> None:
        if not self.reconcile(reader, sweep, time):
            raise ReconcileRequiredError
        for delivery in source.deliveries():
            self.intake(delivery)

    def report(self) -> Summary:
        return summarize(self.database)
