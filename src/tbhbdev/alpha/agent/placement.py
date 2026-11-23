"""Choose a host from fresh, read-only probes."""

from __future__ import annotations

import time
from dataclasses import replace
from random import SystemRandom
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import limits, remote
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from random import Random

    from tbhbdev.alpha.agent.model import Host, RunRequest, Settings


def probe(
    settings: Settings, host: Host, repository: str | None, timeout: float
) -> limits.Reading | str:
    try:
        output = transport.shell(
            settings, host, remote.limits(repository), timeout=timeout
        ).strip()
    except transport.TransportError as exc:
        return str(exc)
    return output if output == "repository mirror missing" else limits.parse(output)


def candidate(
    settings: Settings,
    host: Host,
    run: RunRequest,
    account: str | None,
    deadline: float,
) -> tuple[Host, limits.Reading, float] | str:
    if run.harness not in host.proven_harnesses:
        return f"harness {run.harness} has no proven run"
    if host.models and run.model not in host.models:
        return f"model {run.model} is outside host configuration"
    chosen = host.account if account is None else account
    if chosen not in host.accounts:
        return "requested account is unavailable"
    host = replace(host, account=chosen)
    found = probe(settings, host, run.repo, min(30, deadline - time.monotonic()))
    if isinstance(found, str):
        return found
    score, problems = limits.assess(host, found)
    cap = host.max_runs_per_host
    if found.running >= cap:
        problems.append(f"running runs {found.running} >= cap {cap}")
    if problems:
        return "; ".join(problems)
    return host, found, min(score, 100 * (1 - found.running / cap))


def select(
    settings: Settings,
    run: RunRequest,
    account: str | None,
    deadline: float,
    *,
    rng: Random | None = None,
) -> tuple[Host, limits.Reading, dict[str, str]]:
    reasons: dict[str, str] = {}
    eligible: list[tuple[Host, limits.Reading, float]] = []
    for host in settings.hosts:
        result = candidate(settings, host, run, account, deadline)
        if isinstance(result, str):
            reasons[host.name] = result
        else:
            _, found, score = result
            reasons[host.name] = (
                f"eligible, headroom {score:.3g}, running {found.running}"
            )
            eligible.append(result)
    if not eligible:
        details = "\n".join(f"{name}: {reason}" for name, reason in reasons.items())
        message = f"no host has headroom\n{details}"
        raise Invalid(message)
    best = max(score for _, _, score in eligible)
    random = SystemRandom() if rng is None else rng
    host, found, _ = random.choice([e for e in eligible if e[2] == best])
    return host, found, reasons
