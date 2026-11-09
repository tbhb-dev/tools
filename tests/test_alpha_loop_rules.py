"""Synthetic table rows and individual guard flips for shadow decisions."""

from dataclasses import FrozenInstanceError, replace

import pytest

from tbhbdev.alpha.loop.rules import (
    EscalationArguments,
    FailedJob,
    HarnessCapacity,
    Host,
    Inputs,
    Intent,
    PRArguments,
    PullRequest,
    RepositoryPolicy,
    Run,
    RunArguments,
    RunState,
    SpawnArguments,
    SpawnBudget,
    Sweep,
    Target,
    decide,
)
from tbhbdev.alpha.loop.state import BlockedReason, CIObservation, PullRequestFacts

HEAD = "a" * 40
OTHER = "b" * 40
REPO = "example-org/example-repo"
BASE = f"{REPO}#7"
OBJECT = f"{BASE}@{HEAD}"
FACTS = PullRequestFacts(
    ci=CIObservation("success"),
    merge_state="clean",
    review_runs_at_head=(),
    signatures_required=False,
    head_signature_valid=None,
    required_approvals=1,
    approvals_at_head=0,
    base_repo_verified=True,
    lifecycle="open",
    in_scope=True,
    is_draft=False,
)
PR = PullRequest(
    REPO, 7, HEAD, FACTS, RepositoryPolicy(requires_approval=True, follow_up_steps=())
)
ARGS = PRArguments(REPO, 7, HEAD)
CODEX = Target("example-build.example.com", "codex")
CLAUDE = Target("example-review.example.com", "claude")
BUDGET = SpawnBudget(
    total_cap=3,
    total_active=0,
    per_host_cap=1,
    daily_cap=40,
    spawned_today=0,
    hosts=(Host(CODEX.host, "codex", 0), Host(CLAUDE.host, "claude", 0)),
    harnesses=(
        HarnessCapacity("codex", 0, 2, paused=False),
        HarnessCapacity("claude", 0, 1, paused=False),
    ),
)
RUN = Run("example-run", "codex", "running", 0, 1800)
SWEEP = Sweep("interval", 12, 900, 900, 2002, 2, 2000)
PASSED = replace(
    PR,
    facts=replace(FACTS, review_runs_at_head=("approve",)),
    reviewer_head_sha=HEAD,
    diff_complete=True,
)
DECLINED = replace(PR, facts=replace(FACTS, review_runs_at_head=("comment",)))
APPROVED = replace(PR, facts=replace(FACTS, approvals_at_head=1))
MERGED = replace(PR, facts=replace(FACTS, lifecycle="merged"))
RED = replace(PR, facts=replace(FACTS, ci=CIObservation("failure")))
DRAFT = replace(PR, facts=replace(FACTS, is_draft=True))
R1 = Intent("R1", "spawn", f"review:{OBJECT}", SpawnArguments(ARGS, CODEX))
R2 = Intent("R2", "fixed", f"approve:{OBJECT}", ARGS)
R3 = Intent("R3", "none", "", ARGS)
R4 = Intent("R4", "fixed", f"merge:{OBJECT}", ARGS)
R5 = Intent("R5", "fixed", f"verify:{BASE}", ARGS)


def notice(reason: str) -> str:
    return f"notify:{OBJECT}:{reason}"


@pytest.mark.parametrize(
    ("pr", "expected"),
    [
        (PR, (R1,)),
        (replace(PR, reviewer_runs_without_approval=2), (R1,)),
        (replace(PR, reviewer_runs_without_approval=3), ()),
        (replace(PR, reviewer_runs_without_approval=4), ()),
        (
            replace(
                PR, policy=RepositoryPolicy(requires_approval=False, follow_up_steps=())
            ),
            (),
        ),
        (PASSED, (R2,)),
        (replace(PASSED, reviewer_head_sha=OTHER), ()),
        (replace(PASSED, reviewer_head_sha=None), ()),
        (replace(PASSED, diff_complete=False), ()),
        (replace(PASSED, opened_by="loop_agent"), ()),
        (replace(PASSED, opened_by="loop_process"), ()),
        (
            replace(
                PASSED,
                policy=RepositoryPolicy(requires_approval=False, follow_up_steps=()),
            ),
            (),
        ),
        (replace(PASSED, facts=replace(PASSED.facts, approvals_at_head=1)), (R4,)),
        (replace(PR, opened_by="loop_agent", mechanical_template_matches=True), (R2,)),
        (
            replace(PASSED, opened_by="loop_agent", mechanical_template_matches=True),
            (R2,),
        ),
        (replace(PR, opened_by="loop_agent", mechanical_template_matches=False), (R1,)),
        (
            replace(PR, opened_by="loop_process", mechanical_template_matches=True),
            (R1,),
        ),
        (replace(PR, mechanical_template_matches=True), (R1,)),
        (
            replace(RED, opened_by="loop_agent", mechanical_template_matches=True),
            (Intent("R10", "fixed", notice("ci_red"), replace(ARGS, reason="ci_red")),),
        ),
        (
            DECLINED,
            (
                R3,
                Intent(
                    "R10",
                    "fixed",
                    notice("review_declined"),
                    replace(ARGS, reason="review_declined"),
                ),
            ),
        ),
        (APPROVED, (R4,)),
        (
            replace(
                APPROVED, facts=replace(APPROVED.facts, ci=CIObservation("pending"))
            ),
            (),
        ),
        (replace(APPROVED, facts=replace(APPROVED.facts, is_draft=True)), ()),
        (MERGED, (R5,)),
        (replace(MERGED, verification_passed=True), (R5,)),
        (
            replace(
                MERGED,
                policy=RepositoryPolicy(
                    requires_approval=True, follow_up_steps=("publish",)
                ),
            ),
            (R5,),
        ),
        (
            replace(
                MERGED,
                verification_passed=True,
                policy=RepositoryPolicy(
                    requires_approval=True, follow_up_steps=("publish",)
                ),
            ),
            (
                R5,
                Intent(
                    "R6",
                    "spawn",
                    f"follow:{BASE}:publish",
                    SpawnArguments(ARGS, CODEX, "publish"),
                ),
            ),
        ),
        (replace(PR, facts=replace(FACTS, lifecycle="closed")), ()),
        (replace(PR, facts=replace(FACTS, in_scope=False)), ()),
        (replace(PR, facts=replace(FACTS, base_repo_verified=False)), ()),
        (replace(PR, facts=replace(FACTS, required_approvals=0)), ()),
        (replace(PR, facts=replace(FACTS, ci=CIObservation("absent"))), ()),
        (replace(PR, facts=replace(FACTS, signatures_required=True)), ()),
        (replace(PR, facts=replace(FACTS, review_runs_at_head=("live",))), ()),
    ],
)
def test_pr_rule_table(pr: PullRequest, expected: tuple[Intent, ...]) -> None:
    inputs = Inputs(BUDGET, (pr,))
    assert decide(inputs) == expected
    assert decide(inputs) == expected


@pytest.mark.parametrize(
    ("sweep", "fires"),
    [
        (SWEEP, True),
        (replace(SWEEP, age=901), True),
        (replace(SWEEP, age=899), False),
        (replace(SWEEP, trigger="startup", age=0), True),
        (replace(SWEEP, trigger="reconnect", age=0), True),
        (replace(SWEEP, points_remaining=2001), False),
        (replace(SWEEP, points_remaining=2003), True),
        (replace(SWEEP, estimated_cost=3), False),
        (replace(SWEEP, reserve=2001), False),
        (None, False),
    ],
)
def test_reconcile_guards(sweep: Sweep | None, fires: bool) -> None:
    expected = (Intent("R0", "read", "sweep:12", 12),) if fires else ()
    assert decide(Inputs(BUDGET, sweep=sweep)) == expected


@pytest.mark.parametrize(
    ("pr", "age", "fires"),
    [
        (PR, 1800, False),
        (PR, 1801, True),
        (PASSED, 1800, False),
        (PASSED, 1801, True),
        (APPROVED, 900, False),
        (APPROVED, 901, True),
        (DECLINED, 86400, False),
        (DECLINED, 86401, True),
        (replace(PR, live_run=True), 1801, False),
        (replace(PASSED, live_run=True), 1801, True),
        (MERGED, 100000, False),
    ],
)
def test_r9_state_age(pr: PullRequest, age: int, fires: bool) -> None:
    actual = decide(Inputs(BUDGET, (replace(pr, state_age=age),)))
    if fires:
        assert actual == (
            Intent(
                "R9",
                "fixed",
                f"escalate:{OBJECT}:age",
                EscalationArguments(OBJECT, "age"),
            ),
        )
    else:
        assert all(intent.rule != "R9" for intent in actual)
        assert actual == decide(Inputs(BUDGET, (pr,)))


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
def test_r9_action_failure_limit(count: int) -> None:
    expected = (
        (R1,)
        if count < 3
        else (
            Intent(
                "R9",
                "fixed",
                f"escalate:{OBJECT}:action_failed",
                EscalationArguments(OBJECT, "action_failed"),
            ),
        )
    )
    assert (
        decide(Inputs(BUDGET, (replace(PR, action_failures=(0, count)),))) == expected
    )


@pytest.mark.parametrize("reason", ["dirty", "behind", "unsigned"])
def test_r10_blocked(reason: str) -> None:
    facts = replace(FACTS, merge_state=reason)
    if reason == "unsigned":
        facts = replace(FACTS, signatures_required=True, head_signature_valid=False)
    assert decide(Inputs(BUDGET, (replace(PR, facts=facts),))) == (
        Intent(
            "R10", "fixed", notice(reason), replace(ARGS, reason=BlockedReason(reason))
        ),
    )


@pytest.mark.parametrize(
    ("pr", "fires"),
    [
        (replace(DRAFT, green_age=7201), True),
        (replace(DRAFT, green_age=7200), False),
        (replace(DRAFT, green_age=7199), False),
        (replace(DRAFT, state_age=10000), False),
        (replace(DRAFT, green_age=7201, live_run=True), False),
        (
            replace(
                DRAFT,
                green_age=7201,
                facts=replace(DRAFT.facts, ci=CIObservation("pending")),
            ),
            False,
        ),
        (replace(MERGED, green_age=7201), False),
    ],
)
def test_r10_draft_guards(pr: PullRequest, fires: bool) -> None:
    actual = decide(Inputs(BUDGET, (pr,)))
    expected = Intent("R10", "fixed", notice("draft"), replace(ARGS, reason="draft"))
    assert (expected in actual) is fires


@pytest.mark.parametrize(
    ("jobs", "existing", "rerun"),
    [
        ((FailedJob(HEAD, "cancelled", 12),), frozenset(), True),
        (
            (FailedJob(HEAD, "cancelled", 12), FailedJob(HEAD, "cancelled", 12)),
            frozenset(),
            True,
        ),
        (
            (FailedJob(HEAD, "cancelled", 19), FailedJob(HEAD, "cancelled", 12)),
            frozenset(),
            True,
        ),
        ((), frozenset(), False),
        ((FailedJob(OTHER, "cancelled", 12),), frozenset(), False),
        ((FailedJob(HEAD, "failure", 12),), frozenset(), False),
        ((FailedJob(HEAD, "timed_out", 12),), frozenset(), False),
        (
            (FailedJob(HEAD, "cancelled", 12), FailedJob(HEAD, "failure", 19)),
            frozenset(),
            False,
        ),
        (
            (FailedJob(HEAD, "failure", 19), FailedJob(HEAD, "cancelled", 12)),
            frozenset(),
            False,
        ),
        ((FailedJob(HEAD, "cancelled", 12),), frozenset({f"rerun:{OBJECT}"}), False),
    ],
)
def test_r11_all_failures_must_be_transient(
    jobs: tuple[FailedJob, ...], existing: frozenset[str], rerun: bool
) -> None:
    pr = replace(RED, failed_jobs=jobs)
    if rerun:
        expected = (
            Intent(
                "R11",
                "fixed",
                f"rerun:{OBJECT}",
                replace(
                    ARGS,
                    workflow_run_ids=tuple(
                        sorted({job.workflow_run_id for job in jobs})
                    ),
                ),
            ),
        )
    else:
        expected = (
            Intent("R10", "fixed", notice("ci_red"), replace(ARGS, reason="ci_red")),
        )
    assert decide(Inputs(BUDGET, (pr,), existing_keys=existing)) == expected
    # Failed-job observations cannot rerun a pending, approved or merged PR.
    for other in (PR, APPROVED, MERGED):
        assert decide(Inputs(BUDGET, (replace(other, failed_jobs=jobs),))) == decide(
            Inputs(BUDGET, (other,))
        )


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (RUN, ()),
        (replace(RUN, age=1800), ()),
        (
            replace(RUN, age=1801),
            (
                Intent(
                    "R8",
                    "fixed",
                    "retry:example-run",
                    RunArguments("example-run", CLAUDE),
                ),
            ),
        ),
        (
            replace(RUN, state="starting", age=1801),
            (
                Intent(
                    "R8",
                    "fixed",
                    "retry:example-run",
                    RunArguments("example-run", CLAUDE),
                ),
            ),
        ),
        (
            replace(RUN, state="unknown"),
            (
                Intent(
                    "R8",
                    "fixed",
                    "retry:example-run",
                    RunArguments("example-run", CLAUDE),
                ),
            ),
        ),
        (
            replace(RUN, state="unknown", harness="claude"),
            (
                Intent(
                    "R8",
                    "fixed",
                    "retry:example-run",
                    RunArguments("example-run", CODEX),
                ),
            ),
        ),
        (
            replace(RUN, state="finished", age=1801),
            (
                Intent(
                    "R7", "fixed", "collect:example-run", RunArguments("example-run")
                ),
            ),
        ),
        (replace(RUN, state="failed", age=1801), ()),
        (replace(RUN, state="stopped", age=1801), ()),
        (replace(RUN, state="collected", age=1801), ()),
        (replace(RUN, retries=1), ()),
        (
            replace(RUN, state="finished", retries=1),
            (
                Intent(
                    "R7", "fixed", "collect:example-run", RunArguments("example-run")
                ),
            ),
        ),
        (
            replace(RUN, action_failures=(3,)),
            (
                Intent(
                    "R9",
                    "fixed",
                    "escalate:example-run:action_failed",
                    EscalationArguments("example-run", "action_failed"),
                ),
            ),
        ),
    ],
)
def test_run_rules(run: Run, expected: tuple[Intent, ...]) -> None:
    assert decide(Inputs(BUDGET, runs=(run,))) == expected


@pytest.mark.parametrize(
    "state", ["failed", "stopped", "unknown", "starting", "running"]
)
def test_r9_retry_failure_stops_run(state: RunState) -> None:
    run = replace(RUN, state=state, retries=1, age=1801, action_failures=(3,))
    assert decide(Inputs(BUDGET, runs=(run,))) == (
        Intent(
            "R9",
            "fixed",
            "escalate:example-run:retry_failed",
            EscalationArguments("example-run", "retry_failed"),
        ),
    )


@pytest.mark.parametrize(
    "budget",
    [
        replace(BUDGET, total_active=3),
        replace(BUDGET, total_active=4),
        replace(BUDGET, total_cap=0),
        replace(BUDGET, spawned_today=40),
        replace(BUDGET, spawned_today=41),
        replace(BUDGET, daily_cap=0),
        replace(BUDGET, hosts=()),
        replace(BUDGET, per_host_cap=0),
        replace(BUDGET, hosts=(Host(CODEX.host, "codex", 1),)),
        replace(BUDGET, hosts=(Host(CODEX.host, "codex", 2),)),
        replace(BUDGET, hosts=(Host(CLAUDE.host, "claude", 0),)),
        replace(BUDGET, harnesses=()),
        replace(BUDGET, harnesses=(HarnessCapacity("claude", 0, 1, paused=False),)),
        replace(BUDGET, harnesses=(HarnessCapacity("codex", 0, 2, paused=True),)),
        replace(BUDGET, harnesses=(HarnessCapacity("codex", 2, 2, paused=False),)),
        replace(BUDGET, harnesses=(HarnessCapacity("codex", 3, 2, paused=False),)),
    ],
)
def test_spawn_budget_guards(budget: SpawnBudget) -> None:
    assert decide(Inputs(budget, (PR,))) == ()
    follow = replace(
        MERGED,
        verification_passed=True,
        policy=RepositoryPolicy(requires_approval=True, follow_up_steps=("publish",)),
    )
    assert decide(Inputs(budget, (follow,))) == (R5,)


def test_retry_stop_is_not_blocked_by_capacity() -> None:
    assert decide(
        Inputs(replace(BUDGET, total_cap=0), runs=(replace(RUN, state="unknown"),))
    ) == (Intent("R8", "fixed", "retry:example-run", RunArguments("example-run")),)


@pytest.mark.parametrize(
    ("budget", "expected_count"),
    [
        (BUDGET, 1),
        (replace(BUDGET, per_host_cap=2), 2),
        (replace(BUDGET, per_host_cap=3), 2),
        (
            replace(
                BUDGET,
                per_host_cap=3,
                harnesses=(HarnessCapacity("codex", 0, 3, paused=False),),
            ),
            3,
        ),
        (replace(BUDGET, per_host_cap=3, daily_cap=1), 1),
        (replace(BUDGET, per_host_cap=3, daily_cap=2), 2),
        (replace(BUDGET, per_host_cap=3, hosts=(Host(CODEX.host, "codex", 1),)), 2),
        (
            replace(
                BUDGET,
                total_cap=5,
                total_active=1,
                per_host_cap=3,
                hosts=(Host(CODEX.host, "codex", 1),),
                harnesses=(HarnessCapacity("codex", 1, 4, paused=False),),
            ),
            2,
        ),
        (replace(BUDGET, per_host_cap=3, total_cap=1), 1),
        (replace(BUDGET, per_host_cap=3, total_active=2), 1),
        (replace(BUDGET, per_host_cap=3, spawned_today=39), 1),
        (
            replace(
                BUDGET,
                per_host_cap=3,
                harnesses=(HarnessCapacity("codex", 1, 2, paused=False),),
            ),
            1,
        ),
        (
            replace(
                BUDGET,
                hosts=(
                    Host(CODEX.host, "codex", 1),
                    Host("example-second.example.com", "codex", 0),
                ),
            ),
            1,
        ),
    ],
)
def test_reservations_bound_the_whole_decision(
    budget: SpawnBudget, expected_count: int
) -> None:
    prs = tuple(replace(PR, number=number) for number in range(1, 6))
    intents = decide(Inputs(budget, prs))
    assert len(intents) == expected_count
    assert len({intent.key for intent in intents}) == expected_count


def test_followups_reserve_and_skip_existing_steps() -> None:
    pr = replace(
        MERGED,
        verification_passed=True,
        policy=RepositoryPolicy(
            requires_approval=True, follow_up_steps=("first", "second", "third")
        ),
    )
    inputs = Inputs(
        BUDGET,
        (pr,),
        existing_keys=frozenset({f"verify:{BASE}", f"follow:{BASE}:first"}),
    )
    assert decide(inputs) == (
        Intent(
            "R6",
            "spawn",
            f"follow:{BASE}:second",
            SpawnArguments(ARGS, CODEX, "second"),
        ),
    )


def test_retry_reserves_before_new_reviewer() -> None:
    budget = replace(BUDGET, total_cap=1)
    run = replace(RUN, state="unknown", harness="claude")
    assert decide(Inputs(budget, (PR,), (run,))) == (
        Intent("R8", "fixed", "retry:example-run", RunArguments("example-run", CODEX)),
    )


@pytest.mark.parametrize("pr", [PR, PASSED, APPROVED, MERGED, RED, DECLINED])
def test_replayed_keys_suppress_actions(pr: PullRequest) -> None:
    first = decide(Inputs(BUDGET, (pr,)))
    keys = frozenset(intent.key for intent in first if intent.key)
    expected = tuple(intent for intent in first if not intent.key)
    assert decide(Inputs(BUDGET, (pr,), existing_keys=keys)) == expected


def test_replayed_run_sweep_and_escalation_keys() -> None:
    inputs = Inputs(
        BUDGET,
        (replace(PR, action_failures=(3,)),),
        (replace(RUN, state="finished"),),
        SWEEP,
    )
    first = decide(inputs)
    assert (
        decide(replace(inputs, existing_keys=frozenset(intent.key for intent in first)))
        == ()
    )
    retry = Inputs(
        BUDGET,
        runs=(replace(RUN, state="unknown"),),
        existing_keys=frozenset({"retry:example-run"}),
    )
    assert decide(retry) == ()


def test_stopped_objects_do_not_act() -> None:
    inputs = Inputs(
        BUDGET,
        (PR,),
        (replace(RUN, state="finished"),),
        stopped_objects=frozenset({OBJECT, "example-run"}),
    )
    assert decide(inputs) == ()


def test_empty_and_duplicate_inputs() -> None:
    assert decide(Inputs(BUDGET)) == ()
    assert decide(Inputs(replace(BUDGET, per_host_cap=2), (PR, PR))) == (R1,)


def test_argument_fields_are_structured() -> None:
    assert ARGS.workflow_run_ids == ()
    assert RunArguments("example-run").run_name == "example-run"
    assert RunArguments("example-run").retry_target is None
    red = replace(RED, failed_jobs=(FailedJob(HEAD, "cancelled", 12),))
    result = decide(Inputs(BUDGET, (red,)))
    assert isinstance(result[0].arguments, PRArguments)
    assert result[0].arguments.workflow_run_ids == (12,)


@pytest.mark.parametrize(
    ("observation", "field"),
    [
        (PR, "head_sha"),
        (PR.policy, "requires_approval"),
        (FailedJob(HEAD, "cancelled", 12), "conclusion"),
        (RUN, "age"),
        (BUDGET, "daily_cap"),
        (BUDGET.hosts[0], "active"),
        (BUDGET.harnesses[0], "paused"),
        (SWEEP, "time_slot"),
        (CODEX, "host"),
        (ARGS, "reason"),
        (SpawnArguments(ARGS, CODEX), "step"),
        (RunArguments("example-run"), "retry_target"),
        (EscalationArguments(OBJECT, "age"), "reason"),
        (R1, "key"),
        (Inputs(BUDGET), "existing_keys"),
    ],
)
def test_all_boundary_values_are_frozen(observation: object, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(observation, field, None)
