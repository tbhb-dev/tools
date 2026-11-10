"""Protection precedence and the inclusive age boundary apply on every read."""

import pytest

from tbhbdev.alpha.agent.decisions.prune import Facts, decide


@pytest.mark.parametrize(
    ("keep", "session", "mtime", "force", "now", "days", "reason"),
    [
        pytest.param(
            True, True, None, False, 100, 0, "kept", id="keep-precedes-session"
        ),
        pytest.param(
            True, True, 1, True, 100, 0, "running", id="force-keeps-session-protection"
        ),
        pytest.param(
            False,
            True,
            None,
            False,
            100,
            0,
            "running",
            id="session-precedes-unfinished",
        ),
        pytest.param(
            False,
            False,
            None,
            True,
            100,
            0,
            "unfinished",
            id="force-keeps-completion-protection",
        ),
        pytest.param(
            True, False, 1, True, 100, 0, "prune", id="force-bypasses-keep-only"
        ),
        pytest.param(
            False, False, 100, False, 100, 0, "recent", id="cutoff-is-inclusive"
        ),
        pytest.param(False, False, 101, False, 100, 0, "recent", id="future-is-recent"),
        pytest.param(
            False, False, 99, False, 100, 0, "prune", id="older-by-one-second"
        ),
        pytest.param(
            False, False, 1, False, 86401, 1, "recent", id="day-is-86400-seconds"
        ),
        pytest.param(
            False, False, 0, False, 86401, 1, "prune", id="epoch-zero-is-finished"
        ),
        pytest.param(
            False,
            False,
            0,
            False,
            172800,
            2,
            "recent",
            id="multiply-days-before-subtracting",
        ),
        pytest.param(
            True, False, 1, False, 100, 0, "kept", id="recheck-new-keep-skips"
        ),
        pytest.param(
            False, True, 1, False, 100, 0, "running", id="recheck-new-session-skips"
        ),
        pytest.param(
            False,
            False,
            None,
            False,
            100,
            0,
            "unfinished",
            id="recheck-missing-rc-skips",
        ),
        pytest.param(
            False,
            False,
            100,
            True,
            100,
            0,
            "recent",
            id="recheck-new-rc-skips-even-with-force",
        ),
    ],
)
def test_selection(  # noqa: PLR0913, PLR0917 -- each column is a decision input
    keep: bool,
    session: bool,
    mtime: int | None,
    force: bool,
    now: int,
    days: int,
    reason: str,
) -> None:
    facts = Facts(session, mtime, keep, 4, 91)
    assert decide(facts, now=now, days=days, force=force) == reason
    assert facts == Facts(session, mtime, keep, 4, 91)
