"""Validate workspace settings and decide how host upgrades handle their inputs."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import PurePath

REPO = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")

# What an upgrade step reports.
OK = "ok"
CHANGED = "changed"
FAILED = "failed"

# CANARY matches the line the rules repository's gitleaks runner checks.
GITLEAKS_EXTENDS = ".gitleaks.toml"
GITLEAKS_CANARY = "tbhb-private-canary"
CANARY = re.compile(
    rf"""^[ \t]*id[ \t]*=[ \t]*["']{GITLEAKS_CANARY}["'][ \t]*(?:#.*)?$""",
    re.MULTILINE,
)


class HostError(Exception):
    """A command for agent hosts failed. The message never holds a token."""


@dataclass(frozen=True)
class Workspace:
    """The workspace root, relative to the operator's home, and its clones."""

    root: PurePosixPath
    repos: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    status: str
    message: str


def workspace_settings(
    settings: dict[str, object] | None, source: PurePath
) -> Workspace | None:
    """Validate the [workspace] table from SOURCE."""
    if settings is None:
        return None
    root = settings.get("root")
    path = PurePosixPath(root) if isinstance(root, str) else PurePosixPath("/")
    if path.is_absolute() or not path.parts or ".." in path.parts:
        message = f"{source}: [workspace] root must be a relative path below home"
        raise HostError(message)
    repos = settings.get("repos", [])
    entries: list[object] = repos if isinstance(repos, list) else [None]
    names = [e for e in entries if isinstance(e, str) and REPO.match(e)]
    if len(names) != len(entries):
        message = f"{source}: [workspace] repos must list owner/name strings"
        raise HostError(message)
    return Workspace(path, tuple(names))


def command_error(returncode: int, stderr: str) -> str:
    return stderr.strip() or f"exit {returncode}"


def clone_default_branch(stdout: str, stderr: str, returncode: int) -> str | Result:
    """The branch named by origin/HEAD, or a failure if it cannot be resolved."""
    default = stdout.strip().removeprefix("origin/")
    if returncode != 0 or not default:
        return Result(
            FAILED,
            f"cannot find the default branch: {command_error(returncode, stderr)}",
        )
    return default


def clone_branch_problem(stdout: str, returncode: int, default: str) -> Result | None:
    """Leave a detached clone or a clone on another branch alone."""
    name = stdout.strip()
    if returncode != 0 or name != default:
        return Result(OK, f"left alone: on {name or 'a detached HEAD'}, not {default}")
    return None


def clone_status_problem(stdout: str, stderr: str, returncode: int) -> Result | None:
    """Stop on a failed status check or tracked changes."""
    if returncode != 0:
        return Result(FAILED, f"git status failed: {command_error(returncode, stderr)}")
    if stdout.strip():
        return Result(OK, "left alone: tracked files are modified or staged")
    return None


def clone_revisions(stdout: str, upstream: str) -> tuple[str, str] | Result:
    """The revisions still to compare, or a result if they are missing or equal."""
    heads = stdout.split()
    if len(heads) != 2:  # noqa: PLR2004
        return Result(FAILED, f"cannot resolve HEAD and {upstream}")
    head, tip = heads
    if head == tip:
        return Result(OK, f"up to date at {head[:12]}")
    return head, tip


def gitleaks_config_problem(text: str) -> str | None:
    """Why TEXT isn't the private gitleaks config, or None. It never quotes TEXT."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return "isn't valid TOML"
    extend = data.get("extend")
    if not isinstance(extend, dict) or extend.get("path") != GITLEAKS_EXTENDS:
        return f'has no [extend] path = "{GITLEAKS_EXTENDS}"'
    if not CANARY.search(text):
        return f"has no rule with the id {GITLEAKS_CANARY}"
    return None
