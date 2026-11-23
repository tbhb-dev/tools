"""Report text is a pure function of literal facts."""

from tbhbdev.alpha.agent import render
from tbhbdev.alpha.agent.model import Profile, RunKind


def test_brief_and_launch() -> None:
    assert render.brief(RunKind.EXEC, "example\n", profile=Profile.BUILD) == (
        "Report results only in your final answer. "
        "Never message Claude sessions, session sockets or other agents directly."
        "\nCommit your changes locally, including .github/PULL_REQUEST_DRAFT.md. "
        "The driver pushes the build branch and opens the pull request after success."
        "\nexample\n"
    )
    assert render.brief(RunKind.START, "example", profile=Profile.REVIEW) == "example"


def test_unsandboxed_reports_filter_authorization() -> None:
    reference = "o/r#1\x1b\x07\u200e"
    suffix = " sandbox=none authorized_by=o/r#1"
    assert (
        render.launched("example", "example-linux", "ops", authorized_by=reference)
        == "example: launched host=example-linux account=ops" + suffix
    )
    assert render.status(
        "example",
        phase="running",
        rc="running",
        target=("example-linux", "ops", reference, None),
        started="today",
    ) == (
        "example: phase=running rc=running host=example-linux account=ops started=today"
        " launcher=unknown" + suffix
    )


def test_launch() -> None:
    assert (
        render.launched("example", "example-linux", "agent")
        == "example: launched host=example-linux account=agent"
    )


def test_status() -> None:
    assert render.status(
        "example",
        phase="finishing",
        rc="0",
        target=("example-linux", "agent", None, "coord-review-1"),
        started="2026-11-09T00:00:00+00:00",
    ) == (
        "example: phase=finishing rc=0 host=example-linux "
        "account=agent started=2026-11-09T00:00:00+00:00 launcher=coord-review-1"
    )


def test_launch_on_untrusted_host() -> None:
    assert render.trust(tainted_host=False) == ""
    assert render.trust(tainted_host=True) == " untrusted-host"
    assert (
        render.launched("example", "example-linux", "agent", tainted_host=True)
        == "example: launched host=example-linux account=agent untrusted-host"
    )
