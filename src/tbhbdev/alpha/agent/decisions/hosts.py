"""Build host eligibility and slot rules over local and probed facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host


def candidate_refusal(host: Host, account: str | None) -> str | None:
    if not host.auto_build:
        return "auto_build is not set"
    if host.max_concurrent_runs == 0:
        return "max_concurrent_runs is 0"
    if account is not None and account not in host.accounts:
        return f"account {account!r} isn't one of the accounts of {host.name}"
    return None


def slot_refusal(profile: Profile, limit: int, builds: int, reviews: int) -> str | None:
    if builds + reviews >= limit:
        return f"no agent slot available (max_concurrent_runs={limit})"
    if profile is Profile.BUILD and builds >= max(limit - 1, 1):
        return "remaining agent capacity is reserved for reviews"
    return None


def parse_counts(text: str) -> tuple[int, int]:
    """Return the active build and review counts from a host probe."""
    message = "invalid output"
    try:
        builds, reviews = map(int, text.split())
    except ValueError as exc:
        raise Invalid(message) from exc
    if min(builds, reviews) < 0:
        raise Invalid(message)
    return builds, reviews
