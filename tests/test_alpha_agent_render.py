"""Report text is a pure function of literal facts."""

from tbhbdev.alpha.agent import render
from tbhbdev.alpha.agent.model import Harness, Profile, RunKind


def test_brief_and_launch() -> None:
    final_only = (
        "Report results only in your final answer. "
        "Never message Claude sessions, session sockets or other agents directly."
        "\nKeep any pull request draft out of your commits. "
        "Report any conflicting instruction in your final answer.\n"
    )
    assert render.brief(RunKind.EXEC, "example\n") == final_only + "example\n"
    build = render.brief(RunKind.EXEC, "example\n", profile=Profile.BUILD)
    assert build.startswith(final_only + "Commit your changes locally. ")
    assert "set its `base` frontmatter field" in build
    assert "`gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`" in build
    assert "Push your `agent/` branch" in build
    assert "push --set-upstream origin HEAD" in build
    assert "`tbhbdev alpha pr create` with your draft." in build
    assert ".github/PULL_REQUEST_DRAFT.md" not in build
    assert "driver" not in build
    assert build.endswith("\nexample\n")
    review = render.brief(RunKind.START, "example", profile=Profile.REVIEW)
    assert "`tbhbdev alpha review draft N`" in review
    assert "set `commit_id` to the run's pinned checkout commit" in review
    assert "`event: APPROVE` for GO" in review
    assert "`event: REQUEST_CHANGES` for BLOCK or HOLD" in review
    assert "non-empty Findings for a change request" in review
    assert "In Summary, give one or two sentences of reasons" in review
    assert "Keep the final verdict line in your final answer only" in review
    assert "`tbhbdev alpha review check N --no-lint`" in review
    assert "exactly one `.git/tbhb-alpha/review-N.md`" in review
    assert review.endswith("\nexample")


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
    assert text.index("Keep any pull request draft out of your commits.") < text.index(
        "Commit .github/PULL_REQUEST_DRAFT.md."
    )
    assert "Report any conflicting instruction in your final answer." in text


def test_only_agy_gets_the_no_subagents_rule() -> None:
    """Only agy, which has no subagent control, is asked for none."""
    rule = render.AGY_SUBAGENTS
    for kind, profile in [
        (RunKind.EXEC, Profile.BUILD),
        (RunKind.EXEC, Profile.REVIEW),
        (RunKind.START, Profile.BUILD),
    ]:
        text = render.brief(kind, "task", profile=profile, harness=Harness.AGY)
        assert f"{rule}\ntask" in text
    for harness in (Harness.CODEX, Harness.CLAUDE, None):
        assert rule not in render.brief(RunKind.EXEC, "task", harness=harness)
    assert "`invoke_subagent`" in rule
    assert "`define_subagent`" in rule


def test_only_codex_gets_the_spawn_rule() -> None:
    """Codex forks by default, so its run prompt forbids a fork and asks for a model."""
    rule = render.CODEX_SUBAGENTS
    assert '`fork_turns` to `"none"`' in rule
    assert "name `model` explicitly" in rule
    assert "Never spawn a full-history fork" in rule
    for kind, profile in [
        (RunKind.EXEC, Profile.BUILD),
        (RunKind.EXEC, Profile.REVIEW),
        (RunKind.START, Profile.BUILD),
    ]:
        text = render.brief(kind, "task", profile=profile, harness=Harness.CODEX)
        assert f"{rule}\ntask" in text
        assert render.AGY_SUBAGENTS not in text
    for harness in (Harness.AGY, Harness.CLAUDE, None):
        assert rule not in render.brief(RunKind.EXEC, "task", harness=harness)
