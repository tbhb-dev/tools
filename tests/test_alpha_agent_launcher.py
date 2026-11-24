"""Any named session may start a reviewer-role run."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import Remote, execution_arguments, setup
from agent_example import settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, launcher
from tbhbdev.alpha.agent.decisions import launcher as rules
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from pathlib import Path

# The conftest replaces `launcher.session`; these tests read the real one.
REAL_SESSION = launcher.session

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.settings, "load", settings)
    return result


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("✳ tools", "tools"),
        ("tools", "tools"),
        ("  ⠂ helper-session  ", "helper-session"),
        ("✳", None),
        ("", None),
    ],
)
def test_session_name_drops_the_status_glyph(title: str, name: str | None) -> None:
    assert rules.session_name(title) == name


@pytest.mark.parametrize(
    ("role", "profile", "expected"),
    [
        ("reviewer", Profile.BUILD, True),
        (None, Profile.REVIEW, True),
        ("builder", Profile.BUILD, False),
        (None, Profile.BUILD, False),
    ],
)
def test_a_review_profile_run_is_reviewer_role_too(
    role: str | None, profile: Profile, *, expected: bool
) -> None:
    assert rules.is_reviewer(role, profile) is expected


@pytest.mark.parametrize("launcher_name", ["tools", "helper", "alpha"])
def test_any_named_session_may_launch_reviews(launcher_name: str) -> None:
    rules.check(launcher_name)


def test_a_reviewer_run_without_a_session_name_is_refused() -> None:
    with pytest.raises(Invalid) as caught:
        rules.check(None)
    assert str(caught.value) == (
        "a reviewer-role run needs the launching session's name, from the "
        "tmux pane title; TMUX_PANE or its title is missing, so "
        "name the launching session via the tmux pane title"
    )


@pytest.mark.parametrize("session", ["tools", "example-repo", "helper"])
@pytest.mark.parametrize("profile", ["build", "review"])
def test_a_named_session_can_launch_reviews_for_any_repo(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, session: str, profile: str
) -> None:
    monkeypatch.setattr(launcher, "session", lambda: (session, "s"))
    arguments = execution_arguments(
        remote.prompt, *(("--role", "reviewer") if profile == "build" else ())
    )
    arguments[arguments.index("build")] = profile
    if profile == "review":
        arguments[-2:-2] = ["--ref", "a" * 40]
    assert agent.main(arguments, "example agent") == 0
    run = driver.load(settings(), "sample")
    assert run.launched_by == (session, "s")
    assert run.request.max_concurrent_runs == 3


@pytest.mark.parametrize("profile", ["build", "review"])
def test_a_reviewer_launch_without_a_session_name_is_refused(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
) -> None:
    monkeypatch.setattr(launcher, "session", lambda: (None, "s"))
    arguments = execution_arguments(
        remote.prompt, *(("--role", "reviewer") if profile == "build" else ())
    )
    arguments[arguments.index("build")] = profile
    if profile == "review":
        arguments[-2:-2] = ["--ref", "a" * 40]
    assert agent.main(arguments, "example agent") != 0
    assert (
        "name the launching session via the tmux pane title" in capsys.readouterr().err
    )
    assert not driver.metadata_path("sample").exists()


def test_a_build_launch_without_a_session_name_records_unknown(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launcher, "session", lambda: (None, None))
    assert agent.main(execution_arguments(remote.prompt), "example agent") == 0
    assert agent.main(["status", "sample"], "example agent") == 0
    assert "launcher=unknown" in capsys.readouterr().out
    assert driver.load(settings(), "sample").launched_by == (None, None)


def test_a_record_saved_before_the_launcher_field_loads(remote: Remote) -> None:
    assert agent.main(execution_arguments(remote.prompt), "example agent") == 0
    path = driver.metadata_path("sample")
    record = path.read_text()
    path.write_text(
        record.replace('"launcher": "tools", ', "").replace(
            '"launcher_session": "session-1", ', ""
        )
    )
    assert driver.load(settings(), "sample").launched_by == (None, None)


def reading(
    monkeypatch: pytest.MonkeyPatch, *, title: str | None, session_id: str | None
) -> tuple[str | None, str | None]:
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    if session_id is not None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    if title is not None:
        monkeypatch.setenv("TMUX_PANE", "%9")

        def answer(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, f"{title}\n", "")

        monkeypatch.setattr(launcher.subprocess, "run", answer)
    return REAL_SESSION()


def test_the_session_reads_the_pane_title_and_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert reading(monkeypatch, title="✳ helper", session_id="abc") == (
        "helper",
        "abc",
    )


@pytest.mark.parametrize("title", [None, "", "✳", "  "])
def test_the_session_has_no_name_without_a_pane_or_title(
    monkeypatch: pytest.MonkeyPatch, title: str | None
) -> None:
    assert reading(monkeypatch, title=title, session_id=None) == (None, None)


@pytest.mark.parametrize(
    "error", [OSError("no tmux"), subprocess.TimeoutExpired("t", 5)]
)
def test_the_session_has_no_name_when_tmux_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setenv("TMUX_PANE", "%9")

    def fail(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(launcher.subprocess, "run", fail)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert REAL_SESSION() == (None, None)
