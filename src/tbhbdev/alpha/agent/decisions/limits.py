"""Launch limits over host numbers; no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host

_FIELDS = 9
_PERCENT = 100


@dataclass(frozen=True)
class Reading:
    """Host resources, with memory and swap in KiB and storage in percent."""

    load: float
    cpus: int
    memory_total: int
    memory_available: int
    swap_total: int
    swap_used: int
    disk_free: int
    inode_free: int
    running: int


def parse(text: str) -> Reading | str:
    """The host numbers or the reason the probe could not read them."""
    parts = text.split()
    try:
        if len(parts) != _FIELDS:
            raise ValueError  # noqa: TRY301 -- one cause for every bad shape
        found = Reading(float(parts[0]), *(int(p) for p in parts[1:]))
    except ValueError:
        return "unparsable output"
    if not (
        math.isfinite(found.load)
        and found.load >= 0
        and found.cpus > 0
        and found.memory_total > 0
        and found.memory_available >= 0
        and 0 <= found.swap_used <= found.swap_total
        and 0 <= found.disk_free <= _PERCENT
        and 0 <= found.inode_free <= _PERCENT
        and found.running >= 0
    ):
        return "unparsable output"
    return found


def assess(host: Host, found: Reading) -> tuple[float, list[str]]:
    """The tightest resource margin in percentage points and each failed limit."""
    limit = host.load_limit or found.cpus
    memory = 100 * found.memory_available / found.memory_total
    swap = 100 * found.swap_used / max(1, found.swap_total)
    problems: list[str] = []
    if found.load >= limit:
        problems.append(f"load {found.load:g} >= limit {limit}")
    free = (
        ("memory available", memory, host.min_memory_available_percent),
        ("disk free", found.disk_free, host.min_disk_free_percent),
        ("inodes free", found.inode_free, host.min_inode_free_percent),
    )
    for label, actual, minimum in free:
        if actual < minimum:
            problems.append(f"{label} {actual:.0f}% < {minimum}%")
    if swap > host.max_swap_used_percent:
        problems.append(f"swap used {swap:.0f}% > {host.max_swap_used_percent}%")
    return min(
        100 * (1 - found.load / limit),
        host.max_swap_used_percent - swap,
        *(actual - minimum for _, actual, minimum in free),
    ), problems


def bazel_caps(host: Host, found: Reading) -> tuple[int, int]:
    share = host.max_runs_per_host
    return max(1, found.cpus // share), max(1, found.memory_total // (1024 * share))


def decide(host: Host, found: Reading | str, *, ignore: bool) -> tuple[bool, str]:
    """Whether to refuse the launch, and the message to print, empty if none.

    FOUND is the reading, or the cause of a failed read. A warn-only host and
    IGNORE turn a refusal into a warning.
    """
    if isinstance(found, str):
        problems = [f"cannot read host limits: {found}"]
    else:
        _, problems = assess(host, found)
    if not problems:
        return False, ""
    message = f"{host.name}: " + "; ".join(problems)
    if host.limits_warn_only or ignore:
        return False, f"warning: {message}"
    return True, f"{message}; pass --ignore-limits to launch anyway"
