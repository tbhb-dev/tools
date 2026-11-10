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


def host_identity(host: str) -> None:
    if not HOST_NAME.fullmatch(host):
        message = "invalid taint host or account"
        raise Invalid(message)


def identity(host: str, account: str) -> None:
    host_identity(host)
    if not NAME.fullmatch(account):
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
    reason: str
    time: str

    def __post_init__(self) -> None:
        host_identity(self.host)
        terminal_text(self.reason)
        instant(self.time)


@dataclass(frozen=True)
class State:
    host: str
    status: str = "unknown"
    taint: Taint | None = None
    problem: str = ""

    @property
    def tainted(self) -> bool:
        return self.status != "clean"


def state(records: Iterable[Taint | Clearance], host: str) -> State:
    """Append order decides trust; record times are information only."""
    host_identity(host)
    result = State(host)
    for record in records:
        if record.host == host:
            result = (
                State(host, "tainted", record)
                if isinstance(record, Taint)
                else State(host, "clean")
            )
    return result


@dataclass(frozen=True)
class Launch:
    status: int = 0
    message: str = ""
    label: str | None = None


def check_launch(profile: Profile, *, unsandboxed: bool, tainted: State) -> Launch:
    if profile is Profile.REVIEW and tainted.tainted:
        message = (
            f"review runs refused: {tainted.host} host={tainted.status}; "
            "clear with: tbhbdev alpha agent clear-taint "
            f"{tainted.host} --reason …"
        )
        return Launch(65, message)
    return Launch(label="untrusted-host" if tainted.tainted or unsandboxed else None)


def request_flag(tainted: State) -> bool:
    """Value of the run-host request field `tainted_host`."""
    return tainted.tainted
