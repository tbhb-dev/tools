"""PR size counts from rules and size review statuses from GitHub."""

import json
import subprocess  # nosec B404
from dataclasses import dataclass
from typing import cast

from tbhbdev.alpha.repo_bootstrap import gh_api

COUNTER = ("bazelisk", "run", "//lint:pr_size_count", "--")
COUNT_KEYS = ("code_lines", "code_limit", "raw_lines", "raw_limit")


@dataclass(frozen=True)
class SizeCounts:
    code_lines: int
    code_limit: int
    raw_lines: int
    raw_limit: int
    failed: str | None


class SizeCountError(Exception):
    """The counter could not run or returned an invalid result."""


def size_counts(base: str, head: str) -> SizeCounts:
    """Count added lines between refs using the rules counter.

    Raises:
        SizeCountError: The command is unavailable, fails or returns bad JSON.
    """
    try:
        done = subprocess.run(  # nosec B603
            [*COUNTER, base, head], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise SizeCountError(str(exc)) from exc
    if done.returncode:
        message = done.stderr.strip() or f"counter exited {done.returncode}"
        raise SizeCountError(message)
    try:
        data: object = json.loads(done.stdout)
    except ValueError as exc:
        message = "counter returned invalid JSON"
        raise SizeCountError(message) from exc
    if (
        not isinstance(data, dict)
        or any(type(data.get(key)) is not int for key in COUNT_KEYS)
        or "failed" not in data
        or cast("object", data["failed"]) not in ("code", "raw", None)
    ):
        message = "counter returned invalid size counts"
        raise SizeCountError(message)
    return SizeCounts(
        *(cast("int", data[key]) for key in COUNT_KEYS),
        failed=cast("str | None", data["failed"]),
    )


def has_size_review_success(repo: str, head: str) -> bool:
    """Whether this exact commit has a successful size-review status."""
    data = gh_api("GET", f"repos/{repo}/commits/{head}/status")
    if not isinstance(data, dict) or data.get("sha") != head:
        return False
    statuses = data.get("statuses")
    if not isinstance(statuses, list):
        return False
    return any(
        isinstance(status, dict)
        and status.get("context") == "size-review"
        and status.get("state") == "success"
        for status in cast("list[object]", statuses)
    )
