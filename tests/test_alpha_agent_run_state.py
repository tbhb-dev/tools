"""Lifecycle tables use literal file contents, identities, kinds and times."""

from __future__ import annotations

import pytest

from tbhbdev.alpha.agent import render
from tbhbdev.alpha.agent.decisions import run_state as state
from tbhbdev.alpha.agent.model import Harness, RunKind


@pytest.mark.parametrize(
    ("files", "now", "action"),
    [
        ({}, 9, "prepare"),
        ({"phase": "check"}, 9, "prepare"),
        ({"phase": "workspace"}, 9, "brief"),
        ({"phase": "brief"}, 9, "launch"),
        ({"phase": "running", "launched": ""}, 11, "wait"),
        ({"phase": "exited", "launched": ""}, 11, "stop"),
        ({"phase": "stopped", "launched": ""}, 11, "finish"),
        ({"rc": "0\n"}, 11, "done"),
        ({"rc": "124\n", "phase": "brief"}, 9, "done"),
        ({}, 10, "stop"),
        ({"phase": "workspace"}, 11, "stop"),
        ({"phase": "brief"}, 10, "stop"),
    ],
)
def test_next_action(files: dict[str, str], now: float, action: str) -> None:
    assert state.next_action(files, now=now, startup_deadline=10).value == action


@pytest.mark.parametrize(
    ("files", "expired", "wanted"),
    [
        ({}, False, "wait"),
        ({}, True, "unconfirmed"),
        ({"launched": ""}, True, "launched"),
        ({"launched": ""}, False, "launched"),
        ({"rc": " 0\n", "launched": ""}, False, "0"),
        ({"rc": "124\n"}, True, "124"),
    ],
)
def test_launch_check(files: dict[str, str], expired: bool, wanted: str) -> None:
    assert state.launch_result(files, expired=expired) == wanted


@pytest.mark.parametrize(
    ("kind", "timeout", "maximum", "wanted"),
    [
        ("exec", None, None, None),
        ("exec", 13, None, 13),
        ("exec", None, 2, 120),
        ("exec", 121, 2, 120),
        ("exec", 119, 2, 119),
        ("start", None, 2, None),
        ("start", 200, 2, 200),
        ("start", 1, None, 1),
    ],
)
def test_timeout(
    kind: str, timeout: int | None, maximum: int | None, wanted: int | None
) -> None:
    assert state.time_limit(RunKind(kind), timeout, maximum) == wanted


@pytest.mark.parametrize(
    ("pgid", "current", "wanted"),
    [
        (23, state.Group(23, "boot-a", "42"), True),
        (23, None, False),
        (23, state.Group(24, "boot-a", "42"), False),
        (23, state.Group(23, "boot-b", "42"), False),
        (23, state.Group(23, "boot-a", "43"), False),
        (2, state.Group(2, "boot-a", "42"), True),
        (1, state.Group(1, "boot-a", "42"), False),
        (0, state.Group(0, "boot-a", "42"), False),
    ],
)
def test_ownership(pgid: int, current: state.Group | None, wanted: bool) -> None:
    recorded = state.Group(pgid, "boot-a", "42")
    assert state.owns(recorded, current) is wanted
    assert state.stop_action({}, recorded, current).value == (
        "stop" if wanted else "unknown"
    )
    assert state.stop_action({"rc": "0"}, recorded, current) is state.Action.DONE


@pytest.mark.parametrize(
    ("kind", "files", "live", "wanted"),
    [
        ("start", {}, True, "refuse"),
        ("start", {"rc": "0"}, False, "refuse"),
        ("exec", {}, True, "wait"),
        ("exec", {}, False, "unknown"),
        ("exec", {"rc": " 7\n"}, True, "7"),
        ("exec", {"rc": " 0\n"}, False, "0"),
    ],
)
def test_wait(kind: str, files: dict[str, str], live: bool, wanted: str) -> None:
    assert state.wait_result(RunKind(kind), files, live=live) == wanted


@pytest.mark.parametrize(
    ("files", "force", "wanted"),
    [
        ({}, False, False),
        ({}, True, False),
        ({"phase": "running"}, True, False),
        ({"phase": "unknown"}, False, False),
        ({"phase": "unknown"}, True, True),
        ({"rc": "1"}, False, True),
        ({"rc": "0"}, True, True),
    ],
)
def test_clean(files: dict[str, str], force: bool, wanted: bool) -> None:
    assert state.may_clean(files, force=force) is wanted


@pytest.mark.parametrize(
    ("unpublished", "force", "wanted"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, True),
        (True, True, False),
    ],
)
def test_branch_keep(unpublished: bool, force: bool, wanted: bool) -> None:
    assert state.keep_branch(unpublished=unpublished, force=force) is wanted


@pytest.mark.parametrize(
    ("harness", "before", "after", "wanted"),
    [
        ("codex", None, None, False),
        ("codex", "old", None, False),
        ("codex", "old", "old", False),
        ("codex", None, "new", True),
        ("codex", "old", "new", True),
        ("codex", "old", "", True),
        ("claude", "old", "new", False),
        ("agy", "old", "new", False),
    ],
)
def test_auth(
    harness: str, before: str | None, after: str | None, wanted: bool
) -> None:
    assert state.write_auth(Harness(harness), before, after) is wanted


@pytest.mark.parametrize("kind", RunKind)
def test_brief(kind: RunKind) -> None:
    prefix = (
        "Report results only in your final answer. "
        "Never message Claude sessions, session sockets or other agents directly.\n"
    )
    assert (
        render.brief(kind, "brief text")
        == (prefix if kind is RunKind.EXEC else "") + "brief text"
    )


@pytest.mark.parametrize(
    ("code", "wanted"), [(0, 0), (1, 1), (124, 124), (-1, 129), (-9, 137), (-15, 143)]
)
def test_exit_status(code: int, wanted: int) -> None:
    assert state.exit_status(code) == wanted
