"""Pure validation of untrusted awrel channel attributes and webhook JSON.

Only the allowlisted fields in Event cross this boundary. Delivery metadata is
validated but belongs to the intake layer, not the parsed event. Missing compact
and truncated attributes mean a full, untruncated payload, as in awrel. Rejections
contain schema field names only; neither payload text nor decoder errors escape.
parse_event returns a Rejection for invalid input and leaves logging to the I/O slice.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Never, cast

if TYPE_CHECKING:
    from collections.abc import Collection

type RejectionReason = Literal[
    "missing", "type", "pattern", "unsupported", "mismatch", "truncated", "invalid_json"
]

ACTIONS = {
    "check_suite": frozenset({"completed", "requested", "rerequested"}),
    "check_run": frozenset({"created", "completed", "rerequested", "requested_action"}),
    "pull_request": frozenset(
        {
            "assigned",
            "auto_merge_disabled",
            "auto_merge_enabled",
            "closed",
            "converted_to_draft",
            "demilestoned",
            "dequeued",
            "edited",
            "enqueued",
            "labeled",
            "locked",
            "milestoned",
            "opened",
            "ready_for_review",
            "reopened",
            "review_request_removed",
            "review_requested",
            "synchronize",
            "unassigned",
            "unlabeled",
            "unlocked",
        }
    ),
    "pull_request_review": frozenset({"submitted", "edited", "dismissed"}),
}
CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
)
REVIEW_STATES = frozenset(
    {"approved", "changes_requested", "commented", "dismissed", "pending"}
)
LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?"
REPOSITORY = (
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"
)
SHA = r"[0-9a-f]{40}"
CHECK_NAME = r"[A-Za-z0-9_][A-Za-z0-9 _.,/():+=@#~'*&\[\]-]{0,199}"
DELIVERY = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"


@dataclass(frozen=True)
class Event:
    """Validated fields, preserving matching and compact PR associations."""

    event: str
    repository: str
    action: str
    sender_login: str
    pr_numbers: tuple[int, ...]
    head_sha: str
    check_name: str | None = None
    conclusion: str | None = None
    review_state: str | None = None


@dataclass(frozen=True)
class Rejection:
    field: str
    reason: RejectionReason


class _InvalidError(Exception):
    def __init__(self, field: str, reason: RejectionReason) -> None:
        super().__init__()
        self.rejection = Rejection(field, reason)


def _reject(field: str, reason: RejectionReason) -> Never:
    raise _InvalidError(field, reason)


def _at(data: object, path: str) -> object:
    for key in path.split("."):
        if not isinstance(data, Mapping):
            _reject(path, "type")
        mapping = cast("Mapping[str, object]", data)  # pragma: no mutate (typing only)
        if key not in mapping:
            _reject(path, "missing")
        data = mapping[key]
    return data


def _text(value: object, field: str, pattern: str) -> str:
    if not isinstance(value, str):
        _reject(field, "type")
    if re.fullmatch(pattern, value) is None:
        _reject(field, "pattern")
    return value


def _member(value: object, field: str, choices: frozenset[str]) -> str:
    if not isinstance(value, str):
        _reject(field, "type")
    if value not in choices:
        _reject(field, "unsupported")
    return value


def _number(value: object, field: str) -> int:
    if type(value) is not int:
        _reject(field, "type")
    if not 0 < value <= 2**31 - 1:
        _reject(field, "pattern")
    return value


def _constant(_value: str) -> object:
    # Python's decoder otherwise accepts non-JSON NaN and Infinity literals.
    _reject("body", "invalid_json")


def _matches_repository(data: object, path: str, repository: str) -> bool:
    for key in path.split("."):
        if not isinstance(data, Mapping):
            _reject(path, "type")
        mapping = cast("Mapping[str, object]", data)  # pragma: no mutate (typing only)
        if key not in mapping:
            return True
        data = mapping[key]
    return data == repository


def _checks(
    body: object, kind: str, repository: str
) -> tuple[tuple[int, ...], str, str | None, str | None]:
    def read(name: str) -> object:
        return _at(body, f"{kind}.{name}")

    sha = _text(read("head_sha"), f"{kind}.head_sha", SHA)
    check = cast(
        "Mapping[str, object]",  # pragma: no mutate (typing only)
        _at(body, kind),
    )
    conclusion = check.get("conclusion")
    if conclusion is not None:
        conclusion = _member(conclusion, f"{kind}.conclusion", CONCLUSIONS)
    prs = read("pull_requests")
    if not isinstance(prs, list):
        _reject(f"{kind}.pull_requests", "type")
    items = cast("list[object]", prs)  # pragma: no mutate (typing only)
    numbers = tuple(
        _number(_at(pr, "number"), f"{kind}.pull_requests.number")
        for pr in items
        if _matches_repository(pr, "base.repo.full_name", repository)
    )
    name = None
    if kind == "check_run":
        name = _text(read("name"), "check_run.name", CHECK_NAME)
    return numbers, sha, name, conclusion


def _parse(attributes: object, body: object, watch: Collection[str]) -> Event:
    _member(_at(attributes, "provider"), "provider", frozenset({"github"}))
    kind = _member(_at(attributes, "event"), "event", frozenset(ACTIONS))
    action = _member(_at(attributes, "action"), "action", ACTIONS[kind])
    repository = _text(_at(attributes, "repository"), "repository", REPOSITORY)
    if repository not in watch:
        _reject("repository", "unsupported")
    _text(_at(attributes, "delivery"), "delivery", DELIVERY)
    meta = cast("Mapping[str, object]", attributes)  # pragma: no mutate (typing only)
    for flag in ("compact", "truncated"):
        value = meta.get(flag, "false")
        _member(value, flag, frozenset({"true", "false"}))
        if flag == "truncated" and value == "true":
            _reject(flag, "truncated")
    if not isinstance(body, str):
        _reject("body", "type")
    payload: object = json.loads(body, parse_constant=_constant)
    if _member(_at(payload, "action"), "action", ACTIONS[kind]) != action:
        _reject("action", "mismatch")
    if not _matches_repository(payload, "repository.full_name", repository):
        _reject("repository.full_name", "mismatch")
    sender = _text(_at(payload, "sender.login"), "sender.login", LOGIN)
    name = conclusion = review = None
    if kind in {"check_suite", "check_run"}:
        numbers, sha, name, conclusion = _checks(payload, kind, repository)
    elif kind == "pull_request":
        numbers = (_number(_at(payload, "number"), "number"),)
        sha = _text(_at(payload, "pull_request.head.sha"), "pull_request.head.sha", SHA)
        if not _matches_repository(
            payload, "pull_request.base.repo.full_name", repository
        ):
            _reject("pull_request.base.repo.full_name", "mismatch")
    else:
        numbers = (_number(_at(payload, "pull_request.number"), "pull_request.number"),)
        sha = _text(_at(payload, "review.commit_id"), "review.commit_id", SHA)
        review = _member(_at(payload, "review.state"), "review.state", REVIEW_STATES)
    return Event(
        kind, repository, action, sender, numbers, sha, name, conclusion, review
    )


def parse_event(
    attributes: object, body: object, watch: Collection[str]
) -> Event | Rejection:
    """Validate one awrel event without I/O or interpreting its meaning.

    Attributes use awrel's string values; absent format flags are accepted.
    The delivery repository must belong to watch.
    Fields outside the schema are discarded, even in a full webhook payload.
    """
    try:
        return _parse(attributes, body, watch)
    except _InvalidError as error:
        return error.rejection
    except ValueError, RecursionError:
        return Rejection("body", "invalid_json")
