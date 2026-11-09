# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha gh-shim`: a gh call budget for agent sessions and their scripts.

A PreToolUse hook sees only the command line an agent runs, not the gh calls
a Python script makes. So ``tbhbdev alpha install`` also writes an executable
``gh`` into ``$XDG_DATA_HOME/tbhbdev/agent-bin`` (default ``.local/share``
under HOME), a two-line sh script that runs ``python -I -m tbhbdev alpha
gh-shim "$@"`` with the interpreter that ran the install. That directory goes
first on PATH in agent sessions only:

- Claude Code: the SessionStart hook, ``tbhbdev alpha install --hook``,
  appends ``export PATH=<agent-bin>":$PATH"`` to ``$CLAUDE_ENV_FILE`` once,
  which Claude Code sources before each Bash command of the session.
- Codex and Antigravity have no setting that prepends to PATH (Codex's
  ``shell_environment_policy.set`` takes literal values only), so launch
  them as ``tbhbdev alpha agent-env codex|agy [args...]``. It puts agent-bin
  first on PATH, sets ``TBHB_GH_AGENT=1``, and sets ``TBHB_GH_ROOT_SESSION``
  to a fresh ID unless it's already set, then runs the harness.

Every process the session starts inherits that PATH, so a script's
``subprocess.run(["gh", ...])`` reaches the shim too. The shim finds the real
gh as the first ``gh`` on PATH that isn't a file it wrote.

Outside an agent session it execs the real gh at once: arguments, stdin,
stdout, stderr and the exit status are gh's own. An agent session is one with
any of ``TBHB_GH_ROOT_SESSION``, ``ANTIGRAVITY_CONVERSATION_ID``,
``CODEX_SESSION_ID``, ``CLAUDE_CODE_SESSION_ID``, ``CODEX_THREAD_ID``,
``CLAUDECODE``, ``CODEX_HOME`` or ``TBHB_GH_AGENT`` set, which the harnesses
pass to every child. The first of those, in that order, that holds an ID
names the root session: a Claude subagent shares its root's
``CLAUDE_CODE_SESSION_ID``, and everything under one ``agent-env`` launch
shares one ID. A session with markers but no ID shares one ``unknown`` budget.

Inside an agent session, under a lock in
``$XDG_STATE_HOME/tbhbdev/gh-budget`` (default ``.local/state`` under HOME; the
directory is 0700, the ``lock`` file and the JSON files 0600), it:

1. refuses when the root session already ran ``hourly_limit`` gh calls in the
   past hour (a rolling window, one ``<sha256 of root>.json`` per root);
2. refuses when the account's core REST quota has fewer than ``core_floor``
   requests left. It reads that from ``gh api rate_limit``, which costs no
   quota, at most once a minute for all sessions, caching the answer in
   ``core.json``. The sample runs outside the lock: the call that finds one
   due claims it in the cache, releases the lock, samples, and takes the lock
   again to record the answer and the call. A sample that fails or can't be
   read isn't a refusal: that call skips this check, the cache records no
   quota, and the next sample is due after the minute;
3. records the call and execs the real gh.

The budget is a backstop for polls the hook can't see, not the way to watch
GitHub state: that is the awrel event. A refusal is one line on stderr that
names the budget and tells the agent to subscribe to the awrel event, and exit
status 1. A refused call isn't counted. The optional ``[gh_budget]`` table of
the tbhbdev config file (see ``tbhbdev.userconfig``) sets ``hourly_limit``
(default 300) and ``core_floor`` (default 1000), whole numbers. A config that
can't be read, or a state directory that can't be written, refuses too, with
a line that names the path and the error and doesn't mention polling. State
that isn't valid JSON reads as empty.

This is an accident guard, not a security boundary. It counts gh invocations,
not requests, so one ``gh api --paginate`` is one call (``guard-gh-poll``
blocks those in commands). It misses a gh run by absolute path, a session
whose PATH was replaced or whose markers were removed, a Codex or Antigravity
session not launched through ``agent-env``, other API clients such as curl,
and requests made on another host. The core cache reads the default account
of the inherited environment.
"""

import hashlib
import json
import os
import shlex
import shutil
import subprocess  # nosec B404
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev import userconfig
from tbhbdev.alpha.guard_gh_poll import POLL_REASON
from tbhbdev.alpha.identity import locked, write_private_json

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

type Json = dict[str, object]

PROG = "gh-shim"
SHIM_MARK = "# tbhbdev alpha gh-shim (tbhbdev alpha install)"
ROOT_ENV = "TBHB_GH_ROOT_SESSION"
AGENT_ENV = "TBHB_GH_AGENT"
# Variables whose value names a root session, most specific first.
ROOT_IDS = (
    ROOT_ENV,
    "ANTIGRAVITY_CONVERSATION_ID",
    "CODEX_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
)
# Variables that mark an agent session without naming its root.
MARKERS = ("CLAUDECODE", "CODEX_HOME", AGENT_ENV)
UNKNOWN_ROOT = "unknown"
TABLE = "gh_budget"
DEFAULT_LIMIT = 300
DEFAULT_FLOOR = 1000
HOUR = 3600
CACHE_SECONDS = 60
SAMPLE_TIMEOUT = 15
CORE = "core.json"
REFUSED = 1
NOT_FOUND = 127
# Enough of a file to hold the shim's first two lines.
HEAD = 512


class RefusalError(Exception):
    """The budget refuses this call, with the reason to print."""


def bin_dir() -> Path:
    """The directory the shim is installed in."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "tbhbdev" / "agent-bin"


def state_dir() -> Path:
    """The budget's state directory, shared by every session of the user."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "tbhbdev" / "gh-budget"


def wrapper_text() -> str:
    """The installed ``gh``: run this command with the running interpreter.

    ``-I`` keeps the caller's directory and PYTHON* variables from changing
    what it imports.
    """
    launcher = shlex.join([sys.executable, "-I", "-m", "tbhbdev"])
    return f'#!/bin/sh\n{SHIM_MARK}\nexec {launcher} alpha gh-shim "$@"\n'


def prefixed_path(path: str) -> str:
    """PATH with the shim's directory first, and nowhere else.

    Empty entries go too: they would put the current directory on PATH.
    """
    directory = str(bin_dir())
    rest = [entry for entry in path.split(os.pathsep) if entry not in {"", directory}]
    return os.pathsep.join([directory, *rest])


def is_shim(path: str) -> bool:
    """Whether a file is a gh shim this package wrote."""
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(HEAD)
    except OSError:
        return False
    return SHIM_MARK.encode() in head


def real_gh(env: Mapping[str, str] | None = None) -> str | None:
    """The first executable gh on PATH that isn't a shim.

    ``env`` defaults to the process environment.

    Without PATH, the search uses ``os.defpath``, as exec does. An empty entry
    finds nothing: ``shutil.which`` returns None for path "".
    """
    for entry in os.get_exec_path(env):
        found = shutil.which("gh", path=entry)
        if found and not is_shim(found):
            return found
    return None


def session_root(env: Mapping[str, str]) -> str | None:
    """The root session a call counts against; None outside an agent session."""
    for name in ROOT_IDS:
        if env.get(name):
            return f"{name}={env[name]}"
    if any(env.get(name) for name in MARKERS):
        return UNKNOWN_ROOT
    return None


def whole(section: Json, key: str, default: int) -> int:
    """A [gh_budget] value: a whole number, or the default when it's unset."""
    value = section.get(key, default)
    if type(value) is not int or value < 0:
        message = f"{userconfig.path()}: [{TABLE}] {key} must be a whole number"
        raise userconfig.ConfigError(message)
    return value


def limits() -> tuple[int, int]:
    """(hourly_limit, core_floor) from the config's optional [gh_budget]."""
    if not userconfig.path().is_file():
        return DEFAULT_LIMIT, DEFAULT_FLOOR
    section = userconfig.table(TABLE) or {}
    return (
        whole(section, "hourly_limit", DEFAULT_LIMIT),
        whole(section, "core_floor", DEFAULT_FLOOR),
    )


def read_state(path: Path) -> Json:
    """A state file's object; empty when it's missing or isn't a JSON object."""
    try:
        data: object = json.loads(path.read_text())
    except FileNotFoundError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def recent_calls(path: Path, now: float) -> list[float]:
    """The root's call times within the past hour."""
    calls = read_state(path).get("calls")
    listed: list[object] = calls if isinstance(calls, list) else []
    return [
        float(t) for t in listed if isinstance(t, int | float) and 0 <= now - t < HOUR
    ]


def sample(gh: str) -> int | None:
    """The account's core remaining from ``gh api rate_limit``, or None."""
    try:
        done = subprocess.run(  # nosec B603 -- the resolved gh, fixed arguments
            [gh, "api", "rate_limit"],
            capture_output=True,
            text=True,
            check=True,
            timeout=SAMPLE_TIMEOUT,
        )
        remaining = json.loads(done.stdout)["resources"]["core"]["remaining"]
    except OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError:
        return None
    return remaining if type(remaining) is int else None


def core_state(store: Path, now: float) -> tuple[bool, int | None]:
    """(a sample is due, the cached core remaining or None).

    A sample is due once the cache is a minute old.
    """
    cache = read_state(store / CORE)
    checked = cache.get("checked")
    fresh = isinstance(checked, int | float) and 0 <= now - checked < CACHE_SECONDS
    remaining = cache.get("remaining")
    return not fresh, remaining if type(remaining) is int else None


@contextmanager
def private_lock(store: Path) -> Generator[None]:
    """The store's lock, with the directory at 0700 and the lock file at 0600."""
    store.mkdir(parents=True, exist_ok=True)
    store.chmod(0o700)
    lock = store / "lock"
    lock.touch()
    lock.chmod(0o600)
    with locked(store):
        yield


def within_budget(path: Path, now: float, limit: int) -> list[float]:
    """The root's calls in the past hour, or a refusal when they're at the limit."""
    calls = recent_calls(path, now)
    if len(calls) >= limit:
        message = (
            f"this root session has made {len(calls)} gh calls in the past hour, "
            f"its budget of {limit} per hour"
        )
        raise RefusalError(message)
    return calls


def record(
    path: Path, now: float, limit: int, floor: int, remaining: int | None
) -> None:
    """Count one call, or refuse it; an unknown quota skips the core floor."""
    calls = within_budget(path, now, limit)
    if remaining is not None and remaining < floor:
        message = (
            f"the account has {remaining} core REST requests left this hour, "
            f"under the budget's floor of {floor}"
        )
        raise RefusalError(message)
    write_private_json(path, {"calls": [*calls, now]})


def reserve(gh: str, root: str, now: float, limit: int, floor: int) -> None:
    """Count one call against the root's budget, or refuse it.

    The ``rate_limit`` sample never runs under the lock.
    """
    store = state_dir()
    path = store / f"{hashlib.sha256(root.encode()).hexdigest()}.json"
    core = store / CORE
    with private_lock(store):
        due, remaining = core_state(store, now)
        if not due:
            record(path, now, limit, floor, remaining)
            return
        within_budget(path, now, limit)  # a refused call doesn't sample
        # Claim the sample, keeping the old answer for calls made meanwhile.
        write_private_json(core, {"checked": now, "remaining": remaining})
    sampled = sample(gh)
    with private_lock(store):
        write_private_json(core, {"checked": now, "remaining": sampled})
        record(path, now, limit, floor, sampled)


def main(argv: list[str], _prog: str) -> int:  # noqa: RET503 -- execv doesn't return
    """Check an agent session's budget, then exec the real gh."""
    gh = real_gh()
    if gh is None:
        print(f"{PROG}: no gh on PATH besides the shim", file=sys.stderr)
        return NOT_FOUND
    root = session_root(os.environ)
    if root is not None:
        try:
            limit, floor = limits()
            reserve(gh, root, time.time(), limit, floor)
        except RefusalError as exc:
            print(
                f"{PROG}: refused by the agent gh budget ([{TABLE}] in "
                f"{userconfig.path()}): {exc}. {POLL_REASON}",
                file=sys.stderr,
            )
            return REFUSED
        except (userconfig.ConfigError, OSError) as exc:
            print(
                f"{PROG}: refused, the agent gh budget can't read or write its "
                f"config or state: {exc}",
                file=sys.stderr,
            )
            return REFUSED
    os.execv(gh, [gh, *argv])  # noqa: S606 # nosec B606 -- the resolved gh


def agent_env(argv: list[str], prog: str) -> int:
    """Run a harness with the shim first on PATH and a root session marker."""
    if not argv:
        print(f"usage: {prog} HARNESS [args...]", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["PATH"] = prefixed_path(env.get("PATH", os.defpath))
    env[AGENT_ENV] = "1"
    env.setdefault(ROOT_ENV, uuid.uuid4().hex)
    try:
        os.execvpe(argv[0], argv, env)  # noqa: S606 # nosec B606 -- the operator's
    except OSError as exc:
        print(f"{prog}: cannot run {argv[0]}: {exc}", file=sys.stderr)
        return NOT_FOUND
