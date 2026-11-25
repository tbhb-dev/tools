"""Any named session may start a reviewer-role run."""

from __future__ import annotations

import dataclasses
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
    from collections.abc import Callable
    from pathlib import Path

type SessionName = tuple[str | None, str | None]

# The conftest replaces `launcher.session`; these tests read the real one.
REAL_SESSION = launcher.session

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107


def named(name: str | None, session_id: str | None) -> Callable[..., SessionName]:
    def session(*_: object) -> SessionName:
        return name, session_id

    return session


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
    monkeypatch.setattr(launcher, "session", named(session, "s"))
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
    monkeypatch.setattr(launcher, "session", named(None, "s"))
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
    monkeypatch.setattr(launcher, "session", named(None, None))
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


WORKER = "/opt/worker/bin/worker"


def matched(monkeypatch: pytest.MonkeyPatch, *, found: bool) -> list[str | None]:
    """Make the worker match or not, and record the path each lookup was given."""
    asked: list[str | None] = []

    def refusal(registered: str | None) -> str | None:
        asked.append(registered)
        return None if found else "no ancestor process runs the worker"

    monkeypatch.setattr(launcher.worker_identity, "refusal", refusal)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return asked


def test_a_matched_worker_launches_as_worker_slash_the_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = matched(monkeypatch, found=True)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    assert REAL_SESSION("run:7", WORKER) == ("worker/run:7", "abc")
    assert asked == [WORKER]


def test_an_unmatched_worker_flag_is_refused_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched(monkeypatch, found=False)
    with pytest.raises(Invalid) as caught:
        REAL_SESSION("run:7", WORKER)
    assert str(caught.value) == (
        "--worker-workflow needs the registered worker: "
        "no ancestor process runs the worker"
    )


def test_no_flag_and_no_pane_still_has_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = matched(monkeypatch, found=True)
    assert REAL_SESSION(None, WORKER) == (None, None)
    assert asked == []


def test_the_tmux_pane_keeps_priority_over_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = matched(monkeypatch, found=True)
    assert reading(monkeypatch, title="✳ helper", session_id=None) == (
        "helper",
        None,
    )
    assert REAL_SESSION("run:7", WORKER) == ("helper", None)
    assert asked == []


def test_the_tmux_pane_keeps_priority_over_an_unmatched_worker_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = matched(monkeypatch, found=False)
    reading(monkeypatch, title="✳ helper", session_id=None)
    assert REAL_SESSION("run:7", None) == ("helper", None)
    assert asked == []


def worker_arguments(remote: Remote, *options: str) -> list[str]:
    arguments = execution_arguments(remote.prompt, *options)
    arguments[arguments.index("build")] = "review"
    arguments[-2:-2] = ["--ref", "a" * 40]
    return arguments


def test_a_review_prepares_for_a_matched_worker(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = matched(monkeypatch, found=True)
    monkeypatch.setattr(launcher, "session", REAL_SESSION)
    monkeypatch.setattr(
        agent.settings,
        "load",
        lambda: dataclasses.replace(settings(), worker_path=WORKER),
    )
    arguments = worker_arguments(remote, "--worker-workflow", "run:7")
    assert agent.main(arguments, "example agent") == 0
    assert driver.load(settings(), "sample").launched_by == ("worker/run:7", None)
    assert asked == [WORKER]


def test_the_worker_launcher_shows_in_the_status(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    matched(monkeypatch, found=True)
    monkeypatch.setattr(launcher, "session", REAL_SESSION)
    assert agent.main(worker_arguments(remote, "--worker-workflow", "w1"), "x") == 0
    assert agent.main(["status", "sample"], "example agent") == 0
    assert "launcher=worker/w1" in capsys.readouterr().out


def test_a_review_flag_without_a_matched_worker_is_refused(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    matched(monkeypatch, found=False)
    monkeypatch.setattr(launcher, "session", REAL_SESSION)
    arguments = worker_arguments(remote, "--worker-workflow", "run:7")
    assert agent.main(arguments, "example agent") == 1
    assert capsys.readouterr().err == (
        "example agent: --worker-workflow needs the registered worker: "
        "no ancestor process runs the worker\n"
    )
    assert not driver.metadata_path("sample").exists()


def test_a_non_tmux_review_without_the_flag_fails_as_before(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked = matched(monkeypatch, found=True)
    monkeypatch.setattr(launcher, "session", REAL_SESSION)
    assert agent.main(worker_arguments(remote), "example agent") != 0
    assert (
        "name the launching session via the tmux pane title" in capsys.readouterr().err
    )
    assert asked == []


def test_the_flag_is_refused_for_a_build_without_the_reviewer_role(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked = matched(monkeypatch, found=True)
    monkeypatch.setattr(launcher, "session", REAL_SESSION)
    arguments = execution_arguments(remote.prompt, "--worker-workflow", "w1")
    assert agent.main(arguments, "example agent") == 1
    assert capsys.readouterr().err == (
        "example agent: --worker-workflow applies to reviewer-role runs only\n"
    )
    assert asked == []


def test_a_bad_workflow_id_is_a_usage_error(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = worker_arguments(remote, "--worker-workflow", "a b")
    assert agent.main(arguments, "example agent") == 2
    assert "1 to 128 characters" in capsys.readouterr().err


def test_exec_help_names_the_flag_and_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert agent.main(["exec", "--help"], "example agent") == 0
    text = " ".join(capsys.readouterr().out.split())
    assert (
        "--worker-workflow ID launch as worker/ID: the caller must descend from "
        "the executable in the config's [agent.worker] table (no tmux pane)"
    ) in text


def test_the_session_asks_tmux_for_the_pane_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "helper\n", "")

    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert REAL_SESSION() == ("helper", None)
    assert calls == [
        (
            ["tmux", "display-message", "-p", "-t", "%9", "#{pane_title}"],
            {
                "capture_output": True,
                "text": True,
                "timeout": launcher.TMUX_TIMEOUT_SECONDS,
                "check": True,
            },
        )
    ]
    assert launcher.TMUX_TIMEOUT_SECONDS == 5
