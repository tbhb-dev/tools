"""Pure comparisons and typed shadow counts; no cached object state."""

# cspell:words unscored

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.loop import store

if TYPE_CHECKING:
    from tbhbdev.alpha.loop.rules import PullRequest, Rule
    from tbhbdev.alpha.loop.runner import Snapshot


@dataclass(frozen=True)
class RuleCounts:
    repository: str
    rule: Rule
    intents: int
    agree: int
    disagree: int
    no_change: int
    unscored: int

    @property
    def eligible(self) -> bool:
        return eligible(self.agree + self.disagree)


@dataclass(frozen=True)
class Summary:
    counts: tuple[RuleCounts, ...]
    r9_items: int


MIN_SCORED_INTENTS = 20


def eligible(scored_intents: int) -> bool:
    """Twenty final comparisons qualify for review; disagreements need explanation.

    no_change is provisional and does not count toward promotion. This predicate
    grants no authority to enable a rule; Phase 0 still refuses every on mode.
    """
    return scored_intents >= MIN_SCORED_INTENTS


def score(intent: store.ShadowIntent, snapshot: Snapshot) -> store.Result:
    for effect in snapshot.effects:
        if effect.key == intent.key:
            return effect.result
    if intent.rule == "R0":
        return store.Result.AGREE
    for pr in snapshot.pull_requests:
        base = f"{pr.repository}#{pr.number}"
        obj = intent.key.split(":")[1]
        if obj.split("@")[0] != base:
            continue
        if "@" in obj and obj.split("@")[1] != pr.head_sha:
            return store.Result.DISAGREE
        return _score_pr(intent.rule, pr)
    return store.Result.NO_CHANGE


def _score_pr(rule: Rule, pr: PullRequest) -> store.Result:
    if pr.facts.lifecycle == "closed":
        return store.Result.DISAGREE
    agrees = {
        "R1": bool(pr.facts.review_runs_at_head) or pr.facts.approvals_at_head > 0,
        "R2": pr.facts.approvals_at_head > 0,
        "R4": pr.facts.lifecycle == "merged",
        "R5": pr.verification_passed,
    }.get(rule)
    return store.Result.AGREE if agrees else store.Result.NO_CHANGE


def summarize(database: store.Store) -> Summary:
    groups: dict[tuple[str, Rule], list[store.Result | None]] = {}
    for intent in database.shadow_intents():
        action = database.action(intent.key)
        if action is None:
            raise store.StoreError(store.ErrorCode.CORRUPT)
        groups.setdefault((intent.repository, intent.rule), []).append(action.result)
    counts = tuple(
        RuleCounts(
            repo,
            rule,
            len(results),
            results.count(store.Result.AGREE),
            results.count(store.Result.DISAGREE),
            results.count(store.Result.NO_CHANGE),
            results.count(None),
        )
        for (repo, rule), results in sorted(groups.items())
    )
    return Summary(counts, sum(item.intents for item in counts if item.rule == "R9"))
