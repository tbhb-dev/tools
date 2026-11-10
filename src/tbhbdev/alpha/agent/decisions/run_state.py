"""Run lifecycle decisions from recorded facts, with no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tbhbdev.alpha.agent.model import Harness, RunKind


class Action(StrEnum):
    PREPARE = "prepare"
    BRIEF = "brief"
    LAUNCH = "launch"
    WAIT = "wait"
    STOP = "stop"
    FINISH = "finish"
    DONE = "done"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Group:
    pgid: int
    boot: str
    start: str


def owns(recorded: Group, current: Group | None) -> bool:
    """Whether a live leader still has the recorded identity."""
    return recorded.pgid > 1 and recorded == current


def next_action(
    files: dict[str, str], *, now: float, startup_deadline: float
) -> Action:
    """Advance from literal run-file contents; rc always takes precedence."""
    if "rc" in files:
        return Action.DONE
    if "launched" not in files and now >= startup_deadline:
        return Action.STOP
    return {
        "check": Action.PREPARE,
        "workspace": Action.BRIEF,
        "brief": Action.LAUNCH,
        "running": Action.WAIT,
        "exited": Action.STOP,
        "stopped": Action.FINISH,
    }[files.get("phase", "check")]


def launch_result(files: dict[str, str], *, expired: bool) -> str:
    """The driver's launch check, including a run that completed immediately."""
    if "rc" in files:
        return files["rc"].strip()
    if "launched" in files:
        return "launched"
    return "unconfirmed" if expired else "wait"


def time_limit(kind: RunKind, timeout: int | None, maximum: int | None) -> int | None:
    """The run cap in seconds; the config maximum applies only to exec."""
    limits = [timeout]
    if kind is RunKind.EXEC and maximum is not None:
        limits.append(maximum * 60)
    return min((n for n in limits if n is not None), default=None)


def stop_action(
    files: dict[str, str], recorded: Group, current: Group | None
) -> Action:
    """An ended run never receives another signal; stale ownership is unknown."""
    if "rc" in files:
        return Action.DONE
    return Action.STOP if owns(recorded, current) else Action.UNKNOWN


def exit_status(returncode: int) -> int:
    """Represent a signal exit as the shell's status, for the driver and index."""
    return 128 - returncode if returncode < 0 else returncode


def wait_result(kind: RunKind, files: dict[str, str], *, live: bool) -> str:
    """A wait ends on rc or a missing leader, and refuses interactive runs."""
    if kind is RunKind.START:
        return "refuse"
    if "rc" in files:
        return files["rc"].strip()
    return "wait" if live else "unknown"


def may_clean(files: dict[str, str], *, force: bool) -> bool:
    """Unknown runs require force; a running run cannot be cleaned."""
    return "rc" in files or (files.get("phase") == "unknown" and force)


def keep_branch(*, unpublished: bool, force: bool) -> bool:
    return unpublished and not force


def write_auth(harness: Harness, before: str | None, after: str | None) -> bool:
    """Only a changed Codex copy is written back, under the source lock."""
    return harness is Harness.CODEX and after is not None and before != after
