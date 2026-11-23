"""The recorded build decides who can commission a fresh review."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, NamedTuple

import httpx
import pytest
from agent_driver_example import Remote, execution_arguments, record_build, setup
from agent_example import settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, launcher
from tbhbdev.alpha.agent.decisions import launcher as rules
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile

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


@pytest.fixture
def commissioner(monkeypatch: pytest.MonkeyPatch) -> None:
    def session() -> tuple[str, str]:
        return "example-repo", "commissioner-id"

    monkeypatch.setattr(launcher, "session", session)


@pytest.mark.usefixtures("commissioner")
def test_commissioner_launches_a_fresh_review_for_the_build_branch(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    build = record_build(
        remote.prompt, settings(), "--harness", "claude", "--reason", "build"
    )
    commit = "a" * 40
    ref = f"refs/heads/agent/{build.name}"
    remote.configure(refs=f"{commit}\t{ref}\n")
    arguments = execution_arguments(
        remote.prompt, "--profile", "review", "--ref", f"agent/{build.name}"
    )
    assert agent.main(arguments, "example agent") == 0
    review = driver.load(settings(), "sample")
    assert review.name != build.name
    assert review.request.profile is Profile.REVIEW
    assert review.request.harness is Harness.CODEX
    assert review.request.commit == commit
    assert review.launched_by == build.launched_by
    arguments[-3] = f"agent/{review.name}"
    arguments[-2] = "another-review"
    assert agent.main(arguments, "example agent") == 1
    assert rules.NO_BUILD in capsys.readouterr().err


class RefusedReview(NamedTuple):
    message: str
    build_options: tuple[str, ...] = ()
    review_options: tuple[str, ...] = ()
    session: tuple[str | None, str | None] | None = None
    ref: str | None = "agent/build-example"
    name: str = "sample"


@pytest.mark.parametrize(
    "case",
    [
        RefusedReview("requires a claude"),
        RefusedReview(rules.NO_BUILD, ref="agent/missing"),
        RefusedReview(rules.NO_BUILD, ref="agent/nested/branch"),
        RefusedReview(rules.NO_BUILD, ref="main"),
        RefusedReview(rules.NO_BUILD, build_options=("--repo", "org/other")),
        RefusedReview(
            "only the session that commissioned",
            session=("another-session", "commissioner-id"),
        ),
        RefusedReview(
            "only the session that commissioned", session=("example-repo", "another-id")
        ),
        RefusedReview("launching session's name", session=(None, None)),
        RefusedReview("needs a fresh run name", name="build-example"),
        RefusedReview(
            "supports Codex and Claude builds",
            build_options=("--harness", "agy", "--model", "gemini-example"),
        ),
        RefusedReview(
            "requires --profile review",
            review_options=("--profile", "build", "--role", "reviewer"),
            ref=None,
        ),
    ],
)
@pytest.mark.usefixtures("commissioner")
def test_invalid_review_stops_before_host_contact(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: RefusedReview,
) -> None:
    record_build(remote.prompt, settings(), *case.build_options)
    if case.session is not None:
        identity = case.session

        def caller() -> tuple[str | None, str | None]:
            return identity

        monkeypatch.setattr(launcher, "session", caller)
    arguments = execution_arguments(
        remote.prompt, "--profile", "review", *case.review_options
    )
    if case.ref is not None:
        arguments[-2:-2] = ["--ref", case.ref]
    arguments[-2] = case.name
    assert agent.main(arguments, "example agent") == 1
    assert case.message in capsys.readouterr().err
    assert remote.records("ssh", limits=True) == []


def test_a_previously_used_review_name_is_refused(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    build = record_build(remote.prompt, settings())
    driver.archive_metadata(build.name, "example-stamp")
    arguments = execution_arguments(
        remote.prompt, "--profile", "review", "--ref", f"agent/{build.name}"
    )
    arguments[-2] = build.name
    assert agent.main(arguments, "example agent") == 1
    assert "needs a fresh run name" in capsys.readouterr().err
    assert remote.records("ssh", limits=True) == []


def test_no_recorded_build_stops_review(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = execution_arguments(
        remote.prompt, "--profile", "review", "--ref", "agent/build-example"
    )
    assert agent.main(arguments, "example agent") == 1
    assert rules.NO_BUILD in capsys.readouterr().err
    assert remote.records("ssh", limits=True) == []


@pytest.mark.parametrize(
    ("repository", "status", "head", "expected"),
    [
        (
            "org/repo",
            200,
            {"ref": "agent/build-example", "repo": {"full_name": "ORG/Repo"}},
            "build-example",
        ),
        (
            "org/repo",
            200,
            {
                "ref": "refs/heads/agent/build-example",
                "repo": {"full_name": "ORG/Repo"},
            },
            rules.NO_BUILD,
        ),
        (
            "org/repo",
            200,
            {"ref": "agent/build-example", "repo": {"full_name": "fork/repo"}},
            "must have its head in the same repository",
        ),
        (
            "org/repo",
            200,
            {"ref": "agent/build-example", "repo": {"full_name": None}},
            "must have its head in the same repository",
        ),
        (
            "org/repo",
            200,
            {"ref": None, "repo": {"full_name": "org/repo"}},
            "cannot read the head branch",
        ),
        ("org/repo", 200, {}, "cannot read the head branch"),
        ("org/repo", 404, {}, "cannot read the head branch"),
        ("example/org/repo", 200, {}, "requires the default git host"),
    ],
)
def test_read_only_pull_head_lookup(
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    status: int,
    head: dict[str, object],
    expected: str,
) -> None:
    def get(url: str, *, timeout: float) -> httpx.Response:
        assert url == f"{launcher.API}/repos/org/repo/pulls/3"
        assert timeout > 0
        return httpx.Response(
            status, json={"head": head}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(launcher.httpx, "get", get)
    if expected == "build-example":
        assert launcher.review_build_name(repository, "refs/pull/3/head") == expected
    else:
        with pytest.raises(Invalid, match=expected):
            launcher.review_build_name(repository, "refs/pull/3/head")


def test_a_build_launch_without_a_session_name_records_unknown(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def session() -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(launcher, "session", session)
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

        def answer(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, f"{title}\n", "")

        monkeypatch.setattr(launcher.subprocess, "run", answer)
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
