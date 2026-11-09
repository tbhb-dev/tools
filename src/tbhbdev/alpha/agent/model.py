"""The data types of `tbhbdev alpha agent`: settings, hosts and the run request.

These are plain frozen values. The modules under `decisions/` build and
validate them from values passed in, and no type here reads a file or runs a
process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# The run request's schema version. A reader refuses any other version.
REQUEST_VERSION = 1

# An identifier that reaches a file name, a tmux session name or an argv: run
# names, accounts, profiles and the run's class, variant and role.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# A host name, which may be a dotted ssh name.
HOST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")


class Invalid(ValueError):  # noqa: N818 -- the message names the invalid value
    """A setting, repo argument or run request that fails validation."""


class RunKind(StrEnum):
    EXEC = "exec"
    START = "start"


class Harness(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    AGY = "agy"


class Profile(StrEnum):
    """The sandbox profile of a run."""

    BUILD = "build"
    REVIEW = "review"


class WebSearch(StrEnum):
    LIVE = "live"
    CACHED = "cached"
    DISABLED = "disabled"


class Os(StrEnum):
    LINUX = "linux"
    MACOS = "macos"


@dataclass(frozen=True)
class SandboxPath:
    """The `PATH` entries a run gets on each OS."""

    linux: tuple[str, ...]
    macos: tuple[str, ...]


@dataclass(frozen=True)
class AgySandbox:
    """agy's project grants, relative to the run account's home."""

    review_read: tuple[str, ...]
    build_read: tuple[str, ...]
    build_write: tuple[str, ...]


@dataclass(frozen=True)
class Sandbox:
    write_roots: tuple[str, ...]
    audit_dir: str
    denied_home_paths: tuple[str, ...]
    path: SandboxPath
    agy: AgySandbox


@dataclass(frozen=True)
class Codex:
    """Codex's `web_search` setting for each sandbox profile."""

    web_search_build: WebSearch
    web_search_review: WebSearch


@dataclass(frozen=True)
class Credentials:
    """One account's secret references, each in `vault/item/field` form."""

    account: str
    gh: str
    docker: str | None
    reviews_app_id: str
    reviews_app_client_id: str
    reviews_app_pem: str
    signing: str


@dataclass(frozen=True)
class ClaudeProfile:
    """A Claude auth profile: `name` as config spells it, and two references."""

    name: str
    token: str
    expires: str | None


@dataclass(frozen=True)
class Claude:
    """The profiles, and the name of the one a run uses by default."""

    default_profile: str
    profiles: tuple[ClaudeProfile, ...]


@dataclass(frozen=True)
class Host:
    name: str
    os: Os
    shell: str
    account: str
    unsandboxed: bool
    accounts: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    """The validated `[agent]` table. Durations are in seconds unless named."""

    launch_wait_seconds: int
    agy_timeout_seconds: int
    kill_grace_seconds: int
    max_run_minutes: int | None
    sockets_dir: str
    sandbox: Sandbox
    codex: Codex
    credentials: tuple[Credentials, ...]
    claude: Claude
    hosts: tuple[Host, ...]


@dataclass(frozen=True)
class RunRequest:
    """The versioned run request the driver sends and run-host reads.

    It holds every run field and no credential. `commit` is the resolved commit
    of a review run, and `startup_deadline_seconds` bounds the run's startup.
    The schema version is `REQUEST_VERSION`.
    """

    kind: RunKind
    repo: str
    commit: str | None
    harness: Harness
    model: str
    effort: str
    profile: Profile
    claude_profile: str | None
    reason: str | None
    account: str
    timeout: int | None
    class_: str | None
    variant: str | None
    role: str | None
    unsandboxed: bool
    authorized_by: str | None
    startup_deadline_seconds: int
