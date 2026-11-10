"""Coordinator trust decisions over local records; no host facts or I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import HOST_NAME, NAME, Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Iterable

_TEXT_LIMIT = 200


def terminal_text(value: str) -> None:
    """Apply the driver's single printable line rule to audit text."""
    if not (value.strip() and len(value) <= _TEXT_LIMIT and value.isprintable()):
        message = "audit text must be one printable line of up to 200 characters"
        raise Invalid(message)


def identity(host: str, account: str) -> None:
    if not HOST_NAME.fullmatch(host) or not NAME.fullmatch(account):
        message = "invalid taint host or account"
        raise Invalid(message)


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if "T" not in value or result.utcoffset() != timedelta(0):
        message = "audit time must be UTC ISO-8601"
        raise Invalid(message)
    return result


@dataclass(frozen=True)
class Taint:
    host: str
    account: str
    run: str
    authorized_by: str
    started: str

    def __post_init__(self) -> None:
        identity(self.host, self.account)
        if not NAME.fullmatch(self.run):
            message = "invalid taint run name"
            raise Invalid(message)
        terminal_text(self.authorized_by)
        instant(self.started)


@dataclass(frozen=True)
class Clearance:
    host: str
    account: str
    reason: str
    time: str

    def __post_init__(self) -> None:
        identity(self.host, self.account)
        terminal_text(self.reason)
        instant(self.time)


@dataclass(frozen=True)
class State:
    host: str
    account: str
    taint: Taint | None = None
    problem: str = ""

    @property
    def tainted(self) -> bool:
        return self.taint is not None or bool(self.problem)


def state(
    taints: Iterable[Taint],
    clearances: Iterable[Clearance],
    host: str,
    account: str,
) -> State:
    identity(host, account)
    latest = max(
        (t for t in taints if (t.host, t.account) == (host, account)),
        key=lambda t: instant(t.started),
        default=None,
    )
    cleared = max(
        (instant(c.time) for c in clearances if (c.host, c.account) == (host, account)),
        default=None,
    )
    if latest is not None and (cleared is None or instant(latest.started) > cleared):
        return State(host, account, latest)
    return State(host, account)


@dataclass(frozen=True)
class Launch:
    status: int = 0
    message: str = ""
    label: str | None = None


def check_launch(profile: Profile, *, unsandboxed: bool, tainted: State) -> Launch:
    if profile is Profile.REVIEW and tainted.tainted:
        why = (
            f"ran unsandboxed ({tainted.taint.run})"
            if tainted.taint is not None
            else f"has invalid taint records ({tainted.problem})"
        )
        message = (
            f"review runs refused: {tainted.host} account {tainted.account} {why}; "
            "clear with: tbhbdev alpha agent clear-taint "
            f"{tainted.host} {tainted.account} --reason …"
        )
        return Launch(65, message)
    return Launch(label="untrusted-host" if tainted.tainted or unsandboxed else None)


def request_flag(tainted: State) -> bool:
    """Value of the run-host request field `tainted_host`."""
    return tainted.tainted
