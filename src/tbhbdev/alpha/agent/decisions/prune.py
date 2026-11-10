"""Age pruning decisions over an inventory supplied by the coordinator."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Facts:
    session: bool
    rc_mtime: int | None
    keep: bool
    files: int
    bytes: int


def decide(
    facts: Facts, *, now: float, days: int, force: bool, unsandboxed: bool
) -> str:
    if facts.keep and not force:
        return "kept"
    if facts.session:
        return "running"
    if facts.rc_mtime is None:
        return "unfinished"
    if facts.rc_mtime >= now - days * 86400:
        return "recent"
    if unsandboxed:
        return "unsandboxed"
    return "prune"
