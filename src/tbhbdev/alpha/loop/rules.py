"""Pure R0-R11 decisions over validated observations and immutable receipts.

Q1: green is solely the required conclusion check succeeding at the head.
Provisional recommendations: Q2 permits bounded reconcile and signature reads;
Q3 uses awrel watch in the later intake layer; Q4 permits configured follow-ups;
Q4b permits an exact configured mechanical template for an agent-opened PR,
except one opened by the loop process itself; Q5 permits one escalation item;
Q6 uses event annotations once available, and only cancelled jobs until then.
No rule reads annotations or logs to infer an infrastructure failure today.

Inputs have already crossed validation boundaries. Ages are seconds in the
current state, except green_age which starts when this head turns green.
Counts cover the PR across heads, and existing keys include
intents, completed actions and failures. The caller supplies configuration caps,
ordered host candidates and quota measurements; there are no deployment defaults.
R9 takes precedence and stops the object. Other rules retain table order.
R8 always requests a stop; a missing retry target defers the retry until capacity
exists. Its receipt must remain unfinished until that bounded retry is handled.
All returned actions are shadow intents; this module performs no I/O.
"""

# cspell:words unreviewed

from dataclasses import dataclass
from typing import Literal

from tbhbdev.alpha.loop.state import BlockedReason, PullRequestFacts, State, derive

REVIEW_LIMIT = 3
FAILURE_LIMIT = 3
DRAFT_NOTICE_AGE = 7200

type Harness = Literal["codex", "claude"]
type Rule = Literal[
    "R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11"
]
type Reason = (
    BlockedReason
    | Literal[
        "age", "action_failed", "retry_failed", "review_declined", "ci_red", "draft"
    ]
)
type RunState = Literal[
    "starting", "running", "finished", "failed", "stopped", "unknown", "collected"
]


@dataclass(frozen=True)
class RepositoryPolicy:
    requires_approval: bool
    follow_up_steps: tuple[str, ...]


@dataclass(frozen=True)
class FailedJob:
    head_sha: str
    conclusion: str
    workflow_run_id: int


@dataclass(frozen=True)
class PullRequest:
    repository: str
    number: int
    head_sha: str
    facts: PullRequestFacts
    policy: RepositoryPolicy
    reviewer_runs_without_approval: int = 0
    reviewer_head_sha: str | None = None
    diff_complete: bool = False
    opened_by: Literal["external", "loop_agent", "loop_process"] = "external"
    mechanical_template_matches: bool = False
    verification_passed: bool = False
    state_age: int = 0
    green_age: int = 0
    live_run: bool = False
    action_failures: tuple[int, ...] = ()
    failed_jobs: tuple[FailedJob, ...] = ()


@dataclass(frozen=True)
class Run:
    name: str
    harness: Harness
    state: RunState
    age: int
    time_limit: int
    retries: int = 0
    action_failures: tuple[int, ...] = ()


@dataclass(frozen=True)
class Host:
    name: str
    harness: Harness
    active: int


@dataclass(frozen=True)
class HarnessCapacity:
    harness: Harness
    active: int
    cap: int
    paused: bool


@dataclass(frozen=True)
class SpawnBudget:
    total_cap: int
    total_active: int
    per_host_cap: int
    daily_cap: int
    spawned_today: int
    hosts: tuple[Host, ...]
    harnesses: tuple[HarnessCapacity, ...]


@dataclass(frozen=True)
class Sweep:
    trigger: Literal["startup", "reconnect", "interval"]
    time_slot: int
    age: int
    interval: int
    points_remaining: int
    estimated_cost: int
    reserve: int


@dataclass(frozen=True)
class Target:
    host: str
    harness: Harness


@dataclass(frozen=True)
class PRArguments:
    repository: str
    number: int
    head_sha: str
    reason: Reason | None = None
    workflow_run_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SpawnArguments:
    pr: PRArguments
    target: Target
    step: str | None = None


@dataclass(frozen=True)
class RunArguments:
    run_name: str
    retry_target: Target | None = None


@dataclass(frozen=True)
class EscalationArguments:
    object_key: str
    reason: Reason


@dataclass(frozen=True)
class Intent:
    rule: Rule
    kind: Literal["read", "spawn", "fixed", "none"]
    key: str
    arguments: int | PRArguments | SpawnArguments | RunArguments | EscalationArguments


@dataclass(frozen=True)
class Inputs:
    budget: SpawnBudget
    pull_requests: tuple[PullRequest, ...] = ()
    runs: tuple[Run, ...] = ()
    sweep: Sweep | None = None
    existing_keys: frozenset[str] = frozenset()
    stopped_objects: frozenset[str] = frozenset()


class _Capacity:
    """Reserve within one decision, without changing the supplied snapshot."""

    def __init__(self, budget: SpawnBudget) -> None:
        self.budget = budget
        self.total = budget.total_active
        self.daily = budget.spawned_today
        self.hosts = {host.name: host.active for host in budget.hosts}
        self.harnesses = {item.harness: item.active for item in budget.harnesses}
        self.limits = {item.harness: item for item in budget.harnesses}

    def take(self, harness: Harness) -> Target | None:
        budget = self.budget
        if self.total >= budget.total_cap or self.daily >= budget.daily_cap:
            return None
        item = self.limits.get(harness)
        if item is None or item.paused or self.harnesses[harness] >= item.cap:
            return None
        return self._host(harness)

    def _host(self, harness: Harness) -> Target | None:
        for host in self.budget.hosts:
            if host.harness != harness:
                continue
            if self.hosts[host.name] >= self.budget.per_host_cap:
                continue
            self.total += 1
            self.daily += 1
            self.hosts[host.name] += 1
            self.harnesses[harness] += 1
            return Target(host.name, harness)
        return None


def _failure_reason(failures: tuple[int, ...]) -> Reason | None:
    if any(count >= FAILURE_LIMIT for count in failures):
        return "action_failed"
    return None


def _escalation(object_key: str, reason: Reason | None) -> Intent | None:
    if reason is None:
        return None
    return Intent(
        "R9",
        "fixed",
        f"escalate:{object_key}:{reason}",
        EscalationArguments(object_key, reason),
    )


def _aged(pr: PullRequest, state: State) -> bool:
    if state == State.GREEN_UNREVIEWED and pr.live_run:
        return False
    limit = {
        State.GREEN_UNREVIEWED: 1800,
        State.REVIEW_PASSED: 1800,
        State.APPROVED: 900,
        State.REVIEW_DECLINED: 86400,
    }.get(state)
    return limit is not None and pr.state_age > limit


def _mechanical(pr: PullRequest) -> bool:
    return pr.opened_by == "loop_agent" and pr.mechanical_template_matches


def _can_approve(pr: PullRequest, state: State) -> bool:
    if not pr.policy.requires_approval:
        return False
    if _mechanical(pr):
        return state in {State.GREEN_UNREVIEWED, State.REVIEW_PASSED}
    return (
        state == State.REVIEW_PASSED
        and pr.opened_by == "external"
        and pr.reviewer_head_sha == pr.head_sha
        and pr.diff_complete
    )


def _can_rerun(pr: PullRequest, key: str, existing: frozenset[str]) -> bool:
    return (
        key not in existing
        and bool(pr.failed_jobs)
        and all(
            job.head_sha == pr.head_sha and job.conclusion == "cancelled"
            for job in pr.failed_jobs
        )
    )


def _notice(pr: PullRequest, state: State, *, rerun: bool) -> Reason | None:
    if state == State.BLOCKED:
        return derive(pr.facts).blocked_reason
    if state == State.REVIEW_DECLINED:
        return "review_declined"
    if state == State.CI_RED and not rerun:
        return "ci_red"
    if (
        state == State.DRAFT
        and pr.facts.ci.status == "success"
        and pr.green_age > DRAFT_NOTICE_AGE
        and not pr.live_run
    ):
        return "draft"
    return None


def _pr_actions(pr: PullRequest, inputs: Inputs, capacity: _Capacity) -> list[Intent]:
    base = f"{pr.repository}#{pr.number}"
    obj = f"{base}@{pr.head_sha}"
    state = derive(pr.facts).state
    args = PRArguments(pr.repository, pr.number, pr.head_sha)
    intents: list[Intent] = []
    review_key = f"review:{obj}"
    if (
        state == State.GREEN_UNREVIEWED
        and pr.policy.requires_approval
        and pr.reviewer_runs_without_approval < REVIEW_LIMIT
        and not _mechanical(pr)
        and review_key not in inputs.existing_keys
    ):
        target = capacity.take("codex")
        if target is not None:
            intents.append(
                Intent("R1", "spawn", review_key, SpawnArguments(args, target))
            )
    if _can_approve(pr, state):
        intents.append(Intent("R2", "fixed", f"approve:{obj}", args))
    if state == State.REVIEW_DECLINED:
        intents.append(Intent("R3", "none", "", args))
    # derive only returns APPROVED for an open, green, non-draft PR.
    if state == State.APPROVED:
        intents.append(Intent("R4", "fixed", f"merge:{obj}", args))
    intents.extend(_merge_intents(pr, state, inputs, capacity))
    rerun_key = f"rerun:{obj}"
    rerun = _can_rerun(pr, rerun_key, inputs.existing_keys)
    reason = _notice(pr, state, rerun=rerun)
    if reason is not None:
        intents.append(
            Intent(
                "R10",
                "fixed",
                f"notify:{obj}:{reason}",
                PRArguments(pr.repository, pr.number, pr.head_sha, reason),
            )
        )
    if state == State.CI_RED and rerun:
        run_ids = tuple(sorted({job.workflow_run_id for job in pr.failed_jobs}))
        intents.append(
            Intent(
                "R11",
                "fixed",
                rerun_key,
                PRArguments(
                    pr.repository, pr.number, pr.head_sha, workflow_run_ids=run_ids
                ),
            )
        )
    return intents


def _merge_intents(
    pr: PullRequest, state: State, inputs: Inputs, capacity: _Capacity
) -> list[Intent]:
    if state != State.MERGED:
        return []
    base = f"{pr.repository}#{pr.number}"
    args = PRArguments(pr.repository, pr.number, pr.head_sha)
    intents = [Intent("R5", "fixed", f"verify:{base}", args)]
    if pr.verification_passed:
        for step in pr.policy.follow_up_steps:
            key = f"follow:{base}:{step}"
            if key in inputs.existing_keys:
                continue
            target = capacity.take("codex")
            if target is not None:
                intents.append(
                    Intent("R6", "spawn", key, SpawnArguments(args, target, step))
                )
    return intents


def _pr_intents(pr: PullRequest, inputs: Inputs, capacity: _Capacity) -> list[Intent]:
    obj = f"{pr.repository}#{pr.number}@{pr.head_sha}"
    if obj in inputs.stopped_objects:
        return []
    reason = _failure_reason(pr.action_failures)
    if reason is None and _aged(pr, derive(pr.facts).state):
        reason = "age"
    escalation = _escalation(obj, reason)
    if escalation is not None:
        return [escalation]
    return _pr_actions(pr, inputs, capacity)


def _run_intents(run: Run, inputs: Inputs, capacity: _Capacity) -> list[Intent]:
    if run.name in inputs.stopped_objects:
        return []
    timed_out = run.state in {"starting", "running"} and run.age > run.time_limit
    retry_failed = run.retries > 0 and (
        run.state in {"failed", "stopped", "unknown"} or timed_out
    )
    reason = "retry_failed" if retry_failed else _failure_reason(run.action_failures)
    escalation = _escalation(run.name, reason)
    if escalation is not None:
        return [escalation]
    if run.state == "finished":
        return [Intent("R7", "fixed", f"collect:{run.name}", RunArguments(run.name))]
    key = f"retry:{run.name}"
    if (timed_out or run.state == "unknown") and key not in inputs.existing_keys:
        harness: Harness = "claude" if run.harness == "codex" else "codex"
        return [
            Intent("R8", "fixed", key, RunArguments(run.name, capacity.take(harness)))
        ]
    return []


def decide(inputs: Inputs) -> tuple[Intent, ...]:
    """Return ordered, deduplicated shadow intents, reserving spawn capacity."""
    intents: list[Intent] = []
    sweep = inputs.sweep
    if (
        sweep is not None
        and (sweep.trigger in {"startup", "reconnect"} or sweep.age >= sweep.interval)
        and sweep.points_remaining - sweep.estimated_cost >= sweep.reserve
    ):
        intents.append(
            Intent("R0", "read", f"sweep:{sweep.time_slot}", sweep.time_slot)
        )
    capacity = _Capacity(inputs.budget)
    for run in inputs.runs:
        intents.extend(_run_intents(run, inputs, capacity))
    for pr in inputs.pull_requests:
        intents.extend(_pr_intents(pr, inputs, capacity))
    seen = set(inputs.existing_keys)
    result: list[Intent] = []
    for intent in intents:
        if intent.key and intent.key in seen:
            continue
        seen.add(intent.key)
        result.append(intent)
    return tuple(result)
