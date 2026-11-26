"""Launch limits over host numbers; no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Os

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host

_FIELDS = 7
_UNKNOWN = "-"


@dataclass(frozen=True)
class Reading:
    """The host's 1-minute load and CPU count, with memory and swap in KiB.

    PRESSURE is Linux's memory `some avg10` or macOS's pressure level, and is
    None where the host can't say.
    """

    load: float
    cpus: int
    memory_total: int
    memory_available: int
    swap_total: int
    swap_used: int
    pressure: float | None


def parse(text: str) -> Reading | str:
    """The numbers in TEXT, or the cause of failure when TEXT isn't seven fields.

    The last field is the memory pressure, or `-` when the host can't say.
    """
    parts = text.split()
    try:
        if len(parts) != _FIELDS:
            raise ValueError  # noqa: TRY301 -- one cause for every bad shape
        pressure = None if parts[6] == _UNKNOWN else float(parts[6])
        found = Reading(float(parts[0]), *(int(p) for p in parts[1:6]), pressure)
    except ValueError:
        return "unparsable output"
    sizes = (found.memory_available, found.swap_total, found.swap_used)
    if not (
        math.isfinite(found.load)
        and found.load >= 0
        and found.cpus > 0
        and found.memory_total > 0
        and min(sizes) >= 0
        and (
            found.pressure is None
            or (math.isfinite(found.pressure) and found.pressure >= 0)
        )
    ):
        return "unparsable output"
    return found


def _pressure(host: Host, found: Reading) -> tuple[list[str], list[str]]:
    """The refusals and the warnings that FOUND's pressure and swap raise.

    Swap fill alone only warns while the pressure is known, because Linux keeps
    cold pages in swap long after pressure ends. Where the pressure is unknown
    a full swap still refuses, so nothing gets looser where we can't see.
    """
    swap = ""
    if 100 * found.swap_used > host.max_swap_used_percent * found.swap_total:
        used = 100 * found.swap_used / found.swap_total
        swap = f"swap used {used:.0f}% > {host.max_swap_used_percent}%"
    if found.pressure is None:
        return ([swap] if swap else []), []
    if host.os is Os.MACOS:
        limit, name = host.max_memory_pressure_level, "memory pressure level"
    else:
        limit, name = host.max_memory_pressure_avg10, "memory pressure avg10"
    refusals = []
    if found.pressure >= limit:
        refusals.append(f"{name} {found.pressure:g} >= limit {limit}")
    return refusals, ([swap] if swap else [])


def decide(
    host: Host, found: Reading | str, memory_mib: int, *, ignore: bool
) -> tuple[bool, str]:
    """Whether to refuse the launch, and the message to print, empty if none.

    FOUND is the reading, or the cause of a failed read. A warn-only host and
    IGNORE turn a refusal into a warning.
    """
    notes: list[str] = []
    if isinstance(found, str):
        problems = [f"cannot read host limits: {found}"]
    else:
        limit = found.cpus if host.load_limit is None else host.load_limit
        memory = 100 * found.memory_available / found.memory_total
        problems = []
        if found.load >= limit:
            problems.append(f"load {found.load:g} >= limit {limit}")
        if memory < host.min_memory_available_percent:
            problems.append(
                f"memory available {memory:.0f}% < {host.min_memory_available_percent}%"
            )
        if found.memory_available < memory_mib * 1024:
            problems.append(
                f"memory available {found.memory_available} KiB "
                f"< reservation {memory_mib} MiB"
            )
        refusals, notes = _pressure(host, found)
        problems.extend(refusals)
    if not problems:
        return False, f"warning: {host.name}: " + "; ".join(notes) if notes else ""
    message = f"{host.name}: " + "; ".join(problems + notes)
    if host.limits_warn_only or ignore:
        return False, f"warning: {message}"
    return True, f"{message}; pass --ignore-limits to launch anyway"
