"""Validating the run request, which the driver and run-host both check.

The request is a JSON object with the schema version `v` and every run field.
`parse` reads one, as run-host does, and `validate` checks one the driver
built. Both refuse an unknown version, a field of the wrong format, a Claude
run without its reason or profile, and an `--unsandboxed` run that the rule
doesn't allow.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import NoReturn

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import (
    NAME,
    REQUEST_VERSION,
    Harness,
    Invalid,
    Profile,
    RunKind,
    RunRequest,
)

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_EFFORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_ISSUE = re.compile(rf"{repo.OWNER.pattern}/{repo.REPO.pattern}#[1-9][0-9]*")
_CHAT = re.compile(r"operator-chat-([0-9]{4})-([0-9]{2})-([0-9]{2})")
_REASON_LIMIT = 200
_FIELDS = frozenset(
    {
        "kind",
        "repo",
        "commit",
        "harness",
        "model",
        "effort",
        "profile",
        "claude_profile",
        "reason",
        "account",
        "timeout",
        "class",
        "variant",
        "role",
        "unsandboxed",
        "tainted_host",
        "authorized_by",
        "startup_deadline_seconds",
    }
)


def _fail(field: str, why: str) -> NoReturn:
    message = f"request {field} {why}"
    raise Invalid(message)


def _matches(field: str, value: str | None, pattern: re.Pattern[str]) -> None:
    if value is not None and not pattern.fullmatch(value):
        _fail(field, f"must match {pattern.pattern}")


def _authorization_ok(text: str) -> bool:
    if _ISSUE.fullmatch(text):
        return True
    chat = _CHAT.fullmatch(text)
    if chat is None:
        return False
    try:
        date(int(chat[1]), int(chat[2]), int(chat[3]))
    except ValueError:
        return False
    return True


def _check_unsandboxed(request: RunRequest, *, host_allows: bool) -> None:
    if request.unsandboxed and request.authorized_by is None:
        _fail("unsandboxed", "requires authorized_by")
    if not request.unsandboxed:
        if request.authorized_by is not None:
            _fail("authorized_by", "requires unsandboxed")
        return
    if request.harness is not Harness.CODEX:
        _fail("unsandboxed", "applies only to a codex run")
    if request.profile is not Profile.BUILD:
        _fail("unsandboxed", "applies only to a build run")
    if not host_allows:
        _fail("unsandboxed", "isn't enabled on this host")
    if not _authorization_ok(str(request.authorized_by)):
        _fail("authorized_by", "must be owner/repo#N or operator-chat-YYYY-MM-DD")


def _check_harness(request: RunRequest) -> None:
    claude = request.harness is Harness.CLAUDE
    if claude and request.reason is None:
        _fail("reason", "is required for a claude run")
    if claude and request.claude_profile is None:
        _fail("claude_profile", "is required for a claude run")
    if not claude and request.claude_profile is not None:
        _fail("claude_profile", "applies only to a claude run")
    if (
        request.harness is Harness.AGY
        and request.kind is RunKind.EXEC
        and request.timeout is None
    ):
        _fail("timeout", "is required for an agy exec run")


def _check_commit(request: RunRequest) -> None:
    review = request.profile is Profile.REVIEW
    if review and request.commit is None:
        _fail("commit", "is required for a review run")
    if not review and request.commit is not None:
        _fail("commit", "applies only to a review run")


def validate(request: RunRequest, *, host_allows_unsandboxed: bool) -> None:
    """Refuse REQUEST unless every field and every rule holds.

    HOST_ALLOWS_UNSANDBOXED is the `unsandboxed` key of the run's host in config.

    Raises:
        Invalid: The message names the field.
    """
    repo.parts(request.repo)
    _matches("commit", request.commit, _COMMIT)
    _matches("model", request.model, _MODEL)
    _matches("effort", request.effort, _EFFORT)
    _matches("claude_profile", request.claude_profile, NAME)
    _matches("account", request.account, NAME)
    _matches("class", request.class_, NAME)
    _matches("variant", request.variant, NAME)
    _matches("role", request.role, NAME)
    reason = request.reason
    if reason is not None and not (
        reason.strip() and len(reason) <= _REASON_LIMIT and reason.isprintable()
    ):
        _fail(
            "reason", f"must be one printable line of up to {_REASON_LIMIT} characters"
        )
    if request.timeout is not None and request.timeout <= 0:
        _fail("timeout", "must be a positive number of seconds")
    if request.startup_deadline_seconds <= 0:
        _fail("startup_deadline_seconds", "must be a positive number of seconds")
    _check_commit(request)
    _check_harness(request)
    _check_unsandboxed(request, host_allows=host_allows_unsandboxed)


def to_dict(request: RunRequest) -> dict[str, object]:
    """REQUEST as the JSON object the driver sends."""
    return {
        "v": REQUEST_VERSION,
        "kind": request.kind.value,
        "repo": request.repo,
        "commit": request.commit,
        "harness": request.harness.value,
        "model": request.model,
        "effort": request.effort,
        "profile": request.profile.value,
        "claude_profile": request.claude_profile,
        "reason": request.reason,
        "account": request.account,
        "timeout": request.timeout,
        "class": request.class_,
        "variant": request.variant,
        "role": request.role,
        "unsandboxed": request.unsandboxed,
        "tainted_host": request.tainted_host,
        "authorized_by": request.authorized_by,
        "startup_deadline_seconds": request.startup_deadline_seconds,
    }


def _text(data: dict[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        _fail(key, "must be a string")
    return value


def _maybe_text(data: dict[str, object], key: str) -> str | None:
    return None if data[key] is None else _text(data, key)


def _number(data: dict[str, object], key: str) -> int:
    value = data[key]
    if type(value) is not int:
        _fail(key, "must be an integer")
    return value


def _maybe_number(data: dict[str, object], key: str) -> int | None:
    return None if data[key] is None else _number(data, key)


def _flag(data: dict[str, object], key: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        _fail(key, "must be true or false")
    return value


def _choice[E: StrEnum](data: dict[str, object], key: str, kind: type[E]) -> E:
    members = {member.value: member for member in kind}
    value = data[key]
    if not isinstance(value, str) or value not in members:
        _fail(key, f"must be one of {', '.join(members)}")
    return members[value]


def parse(data: dict[str, object], *, host_allows_unsandboxed: bool) -> RunRequest:
    """The request DATA holds, the JSON object that `to_dict` writes.

    DATA must hold `v` and every field, and no other. `validate` then checks
    the request.

    Raises:
        Invalid: The version is unknown, a field is missing, unknown or of the
            wrong type, or `validate` refuses the request.
    """
    if "v" not in data:
        _fail("v", "is required")
    version = data["v"]
    if type(version) is not int or version != REQUEST_VERSION:
        _fail(
            "v",
            f"{version!r} is not a supported request version; "
            f"this build reads {REQUEST_VERSION}",
        )
    data = {"tainted_host": False, **data}
    missing = _FIELDS - data.keys()
    if missing:
        message = f"request is missing fields: {', '.join(sorted(missing))}"
        raise Invalid(message)
    unknown = data.keys() - _FIELDS - {"v"}
    if unknown:
        message = f"request has unknown fields: {', '.join(sorted(unknown))}"
        raise Invalid(message)
    request = RunRequest(
        kind=_choice(data, "kind", RunKind),
        repo=_text(data, "repo"),
        commit=_maybe_text(data, "commit"),
        harness=_choice(data, "harness", Harness),
        model=_text(data, "model"),
        effort=_text(data, "effort"),
        profile=_choice(data, "profile", Profile),
        claude_profile=_maybe_text(data, "claude_profile"),
        reason=_maybe_text(data, "reason"),
        account=_text(data, "account"),
        timeout=_maybe_number(data, "timeout"),
        class_=_maybe_text(data, "class"),
        variant=_maybe_text(data, "variant"),
        role=_maybe_text(data, "role"),
        unsandboxed=_flag(data, "unsandboxed"),
        tainted_host=_flag(data, "tainted_host"),
        authorized_by=_maybe_text(data, "authorized_by"),
        startup_deadline_seconds=_number(data, "startup_deadline_seconds"),
    )
    validate(request, host_allows_unsandboxed=host_allows_unsandboxed)
    return request
