"""Only a session on the review-launcher list starts a reviewer-role run."""

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
        ("coord-review-1", "coord-review-1"),
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


LAUNCHERS = {**rules.REVIEW_LAUNCHERS, "helper": frozenset({"org/one"})}
OWNERS = {"two-hidden": "alpha"}


@pytest.mark.parametrize(
    ("launcher_name", "repo", "fragment"),
    [
        (None, "o/rules", "TMUX_PANE or its title is missing"),
        ("tools", "o/rules", "may not launch a reviewer-role run"),
        ("helper", "o/rules", "only for org/one"),
        ("coord-review-1", "o/rules", None),
        ("helper", "org/one", None),
        ("alpha", "o/two-hidden", "owns o/two-hidden"),
        ("coord-review-1", "o/two-hidden", None),
    ],
)
def test_the_launcher_list_gates_reviewer_runs(
    launcher_name: str | None, repo: str, fragment: str | None
) -> None:
    launchers = {**LAUNCHERS, "alpha": None}
    if fragment is None:
        rules.check(launcher_name, repo, launchers, OWNERS)
        return
    with pytest.raises(Invalid, match=fragment) as caught:
        rules.check(launcher_name, repo, launchers, OWNERS)
    assert "ask coord" in str(caught.value)


def test_the_built_in_launchers_apply_without_a_table() -> None:
    rules.check("coord-review-2", "o/rules")
    with pytest.raises(Invalid, match="may not launch"):
        rules.check("tools", "o/rules")


@pytest.mark.parametrize(
    ("repo", "owner"), [("o/rules", "rules"), ("o/two-hidden", "alpha")]
)
def test_repository_owner_sessions(repo: str, owner: str) -> None:
    assert rules.owner(repo, OWNERS) == owner


def test_a_missing_table_adds_nothing() -> None:
    assert launcher.load_table() == (rules.REVIEW_LAUNCHERS, {})


def test_the_table_adds_launchers_and_owners() -> None:
    path = launcher.table_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"launchers": {"helper": ["org/one"], "any": null},'
        ' "owners": {"two-hidden": "alpha"}}'
    )
    launchers, owners = launcher.load_table()
    assert launchers["helper"] == frozenset({"org/one"})
    assert launchers["any"] is None
    assert launchers["coord-review-1"] is None
    assert owners == {"two-hidden": "alpha"}


@pytest.mark.parametrize("text", ["not json", "[1]", '{"launchers": [1]}'])
def test_a_malformed_table_is_refused(text: str) -> None:
    path = launcher.table_path()
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(Invalid, match="invalid review-launcher table"):
        launcher.load_table()


def test_a_reviewer_launch_from_an_unlisted_session_is_refused(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launcher, "session", lambda: ("tools", "s"))
    arguments = execution_arguments(remote.prompt, "--role", "reviewer")
    assert agent.main(arguments, "example agent") != 0
    assert "ask coord" in capsys.readouterr().err
    assert not driver.metadata_path("sample").exists()


def test_a_review_profile_launch_without_a_session_name_is_refused(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launcher, "session", lambda: (None, "s"))
    arguments = execution_arguments(remote.prompt)
    arguments[arguments.index("build")] = "review"
    arguments[-2:-2] = ["--ref", "a" * 40]
    assert agent.main(arguments, "example agent") != 0
    assert "launching session's name" in capsys.readouterr().err


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
        record.replace('"launcher": "coord-review-1", ', "").replace(
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
        monkeypatch.setattr(
            launcher.subprocess,
            "run",
            lambda *_, **__: subprocess.CompletedProcess([], 0, f"{title}\n", ""),
        )
    return REAL_SESSION()


def test_the_session_reads_the_pane_title_and_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert reading(monkeypatch, title="✳ coord-review-2", session_id="abc") == (
        "coord-review-2",
        "abc",
    )


def test_the_session_has_no_name_without_a_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert reading(monkeypatch, title=None, session_id=None) == (None, None)


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
