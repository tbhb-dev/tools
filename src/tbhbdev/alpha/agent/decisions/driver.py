"""Coordinator rules over gathered values; no I/O or host-side lifecycle rules."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import config, request
from tbhbdev.alpha.agent.model import NAME, Harness, Invalid, Profile, RunKind

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, RunRequest, Settings

COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        message = f"run name {name!r} must match {NAME.pattern}"
        raise Invalid(message)


def select_host(settings: Settings, name: str, account: str | None) -> Host:
    for host in settings.hosts:
        if host.name == name:
            chosen = host.account if account is None else account
            if chosen not in host.accounts:
                message = f"account {chosen!r} isn't one of the accounts of {name}"
                raise Invalid(message)
            return replace(host, account=chosen)
    message = f"host {name!r} isn't configured"
    raise Invalid(message)


def check(
    run: RunRequest,
    settings: Settings,
    host: Host,
    *,
    ref: str | None,
    exists: bool,
) -> RunRequest:
    if exists:
        message = "run metadata already exists; clean first"
        raise Invalid(message)
    if ref is not None and run.profile is Profile.BUILD:
        message = "--ref applies only to a review run"
        raise Invalid(message)
    if run.harness is Harness.CLAUDE:
        name = (
            settings.claude.default_profile
            if run.claude_profile is None
            else run.claude_profile
        )
        profile = config.find_profile(settings.claude.profiles, name)
        if profile is None:
            message = f"claude profile {name!r} isn't defined"
            raise Invalid(message)
        run = replace(run, claude_profile=profile.name)
    request.validate(run, host_allows_unsandboxed=host.unsandboxed)
    return run


def same_version(local: str, remote: str) -> None:
    if local != remote:
        message = f"version mismatch: driver {local!r}; host {remote!r}"
        raise Invalid(message)


def resolve_ref(ref: str, text: str) -> str:
    if COMMIT.fullmatch(ref):
        return ref
    lines = [line.split() for line in text.splitlines()]
    # Prefer a peeled tag; a branch and tag may name the same commit.
    records = {name: sha for sha, name in lines}
    commits = {sha for name, sha in records.items() if name + "^{}" not in records}
    if len(commits) != 1 or not COMMIT.fullmatch(next(iter(commits))):
        message = f"ref {ref!r} doesn't resolve to one commit"
        raise Invalid(message)
    return commits.pop()


def launch(rc: int | None, *, launched: bool, expired: bool) -> str:
    if rc is not None:
        return "finished"
    if launched:
        return "launched"
    return "deadline" if expired else "poll"


def state(rc: int | None, *, session: bool) -> str:
    if rc is not None:
        return str(rc)
    return "running" if session else "unknown"


def wait(
    rc: int | None, *, session: bool, failed_seconds: float, unreachable: float
) -> int | None:
    if failed_seconds >= unreachable:
        return 75
    if rc is not None:
        return rc
    return None if session else 1


def kill(rc: int | None, *, session: bool, expired: bool) -> str:
    if rc is not None:
        return "finished"
    if not expired:
        return "poll"
    return "refuse" if session else "write"


def clean(rc: int | None, *, session: bool, force: bool) -> None:
    if session:
        message = "run is running; clean refused"
        raise Invalid(message)
    if rc is None and not force:
        message = "run is unknown; clean requires --force"
        raise Invalid(message)


def delete_branch(*, unpushed: bool, force: bool) -> bool:
    return force or not unpushed


def wait_kind(kind: RunKind) -> None:
    if kind is not RunKind.EXEC:
        message = "wait applies only to an exec run"
        raise Invalid(message)


def wait_seconds(value: str, name: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        message = f"{name} must be positive seconds"
        raise Invalid(message) from exc
    if seconds <= 0 or not math.isfinite(seconds):
        message = f"{name} must be positive seconds"
        raise Invalid(message)
    return seconds


def check_model(harness: Harness, model: str, *, allow_non_gemini: bool) -> None:
    if (
        harness is Harness.AGY
        and not model.startswith("gemini-")
        and not allow_non_gemini
    ):
        message = "agy requires a gemini-* model or --allow-non-gemini"
        raise Invalid(message)


def answer_state(rc: int | None) -> str:
    return "unknown" if rc is None else str(rc)
