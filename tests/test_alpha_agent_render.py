"""Report text is a pure function of literal facts."""

from tbhbdev.alpha.agent import render
from tbhbdev.alpha.agent.model import Profile, RunKind


def test_brief_and_launch() -> None:
    final_only = (
        "Report results only in your final answer. "
        "Never message Claude sessions, session sockets or other agents directly."
        "\nIf this run should open a pull request, write .github/PULL_REQUEST_DRAFT.md "
        "in the worktree and keep it out of your commits. "
        "Any brief instruction to commit the draft does not apply: "
        "keep the draft out of commits and report the conflict in your final answer.\n"
    )
    assert render.brief(RunKind.EXEC, "example\n") == final_only + "example\n"
    build = render.brief(RunKind.EXEC, "example\n", profile=Profile.BUILD)
    assert build == (
        final_only + "Commit your changes locally. "
        "The driver pushes the build branch and opens the pull request after success."
        "\nexample\n"
    )
    assert "including" not in build
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
        target=("example-linux", "agent", None, "tools"),
        started="2026-11-09T00:00:00+00:00",
    ) == (
        "example: phase=finishing rc=0 host=example-linux "
        "account=agent started=2026-11-09T00:00:00+00:00 launcher=tools"
    )


def test_build_brief_overrides_an_instruction_to_commit_the_draft() -> None:
    text = render.brief(
        RunKind.EXEC, "Commit .github/PULL_REQUEST_DRAFT.md.", profile=Profile.BUILD
    )
    assert "Any brief instruction to commit the draft does not apply: " in text
    assert (
        "keep the draft out of commits and report the conflict in your final answer."
        in text
    )
