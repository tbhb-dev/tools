"""Preparation failures identify their step and a safe, bounded cause."""

from __future__ import annotations

import subprocess
import time
from functools import partial
from pathlib import Path

import pytest
from agent_host_example import constant, preparation_failure, run_request

from tbhbdev.alpha.agent import process_group, runfiles, worktree
from tbhbdev.alpha.agent.model import Invalid

PUBLIC = ("github.com/tbhb-dev/example-repo",)
URL = "https://github.com/tbhb-dev/example-repo.git"


def test_outside_repository_has_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def unexpected(*_args: object) -> str:
        pytest.fail("unlisted repository must never invoke git")

    monkeypatch.setattr(worktree, "git", unexpected)
    before = list(tmp_path.rglob("*"))
    with pytest.raises(Invalid) as caught:
        worktree.create(
            tmp_path, "example", run_request(repo="example-org/example-repo")
        )
    assert str(caught.value) == (
        "repository example-org/example-repo: agent mirrors fetch only tbhb-dev "
        "repositories"
    )
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("name", "reaches_fetch"),
    [
        ("tbhb-dev/new-repo", True),
        ("github.com/TBHB-Dev/new-repo", True),
        ("git.example.com/tbhb-dev/new-repo", False),
        ("tbhb-dev-other/new-repo", False),
    ],
)
def test_org_repositories_need_no_list_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    reaches_fetch: bool,
) -> None:
    def reached(_clone: Path) -> None:
        message = "reached the clone directory"
        raise Invalid(message)

    monkeypatch.setattr(worktree, "ensure_clone_directory", reached)
    match = "reached the clone" if reaches_fetch else "only tbhb-dev repositories"
    with pytest.raises(Invalid, match=match):
        worktree.create(tmp_path, "example", run_request(repo=name))


@pytest.mark.parametrize("existing", [False, True])
def test_public_mirror_uses_https_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, existing: bool
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    request = run_request()
    mirror = worktree.mirror_path(tmp_path, request.repo)
    if existing:
        mirror.mkdir(parents=True)
        (mirror / "config").write_text(
            '[remote "origin"]\nurl = git@github.com:tbhb-dev/example-repo.git\n'
        )
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def git(path: Path, *args: str) -> str:
        calls.append((path, args))
        if args[0] != "init":
            assert (mirror / "config").read_text() == worktree.MIRROR_CONFIG.format(
                url=URL
            )
        if args[0] == "ls-remote":
            return "ref: refs/heads/main\tHEAD"
        return "a" * 40 if args[0] == "rev-parse" else ""

    monkeypatch.setattr(worktree, "git", git)
    monkeypatch.setattr(worktree, "install_hooks", constant(None))
    monkeypatch.setattr(worktree, "prepare_git", constant(None))
    workspace = worktree.create(tmp_path, "example", request)
    assert workspace == tmp_path / ".worktrees" / "agent-example"
    assert (
        (mirror, ("init", "--bare", "--template=")) in calls
        if not existing
        else (all(args[0] != "init" for _path, args in calls))
    )
    assert all(args[:2] != ("config", "remote.origin.url") for _p, args in calls)
    assert (workspace, ("config", "remote.origin.pushurl", URL)) in calls
    assert any(args[0] == "fetch" for _path, args in calls)


@pytest.mark.parametrize(
    ("target", "step"),
    [
        ("private_dir", "mirror directory"),
        ("configure_mirror", "mirror init"),
        ("fetch_commit", f"mirror fetch ({URL})"),
        ("clone_run", "clone"),
        ("install_hooks", "hook install"),
        ("git", "checkout"),
        ("prepare_git", "git metadata"),
    ],
)
@pytest.mark.parametrize(
    "failure",
    [
        (OSError, "[redacted]"),
        (ValueError, "invalid value"),
        (Invalid, "[redacted]"),
        (subprocess.CalledProcessError, "[redacted]"),
    ],
)
def test_worktree_failure_names_its_step_and_safe_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    step: str,
    failure: tuple[type[Exception], str],
) -> None:
    error, cause = failure
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("EXAMPLE_CREDENTIAL", "sensitive-exception")
    monkeypatch.setenv("EXAMPLE_STDERR", "sensitive-stderr")
    monkeypatch.setattr(worktree, "git", constant(""))
    monkeypatch.setattr(worktree, "configure_mirror", constant(None))
    monkeypatch.setattr(worktree, "fetch_commit", constant("a" * 40))
    monkeypatch.setattr(worktree, "clone_run", constant(None))
    monkeypatch.setattr(worktree, "install_hooks", constant(None))
    monkeypatch.setattr(worktree, "prepare_git", constant(None))

    fail = partial(preparation_failure, error)

    monkeypatch.setattr(
        worktree.runfiles if target == "private_dir" else worktree, target, fail
    )
    with pytest.raises(Invalid) as caught:
        worktree.create(tmp_path, "example", run_request())
    assert str(caught.value) == f"run preparation failed at {step}: {cause}"
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (
            Invalid("host executable prek was not found"),
            "host executable prek was not found",
        ),
        (
            OSError(13, "Permission denied", "/home/example/private/hooks"),
            "Permission denied: hooks",
        ),
        (
            OSError("cannot read /home/example/private/commondir"),
            "cannot read commondir",
        ),
        (OSError("cannot read /root"), "cannot read root"),
        (OSError(5, "Input/output error"), "Input/output error"),
        (
            OSError(5, "Input/output error", b"/home/example/private/HEAD"),
            "Input/output error: HEAD",
        ),
        (ValueError("sensitive-input"), "invalid value"),
        (subprocess.SubprocessError("sensitive-command"), "git command failed"),
        (subprocess.CalledProcessError(1, "sensitive-command"), "git command failed"),
        (
            subprocess.CalledProcessError(1, "sensitive-command", stderr=""),
            "git command failed",
        ),
        (
            subprocess.CalledProcessError(
                1, "sensitive-command", stderr="fatal: no access\nprivate detail"
            ),
            "fatal: no access",
        ),
        (
            subprocess.TimeoutExpired(
                "sensitive-command", 5, stderr=b"fatal: bad \xff\nprivate detail"
            ),
            "fatal: bad \ufffd",
        ),
        (Invalid("bad\x00\t\x1b\x7f\u200b value"), "bad value"),
        (Invalid(""), "cause unavailable"),
        (Invalid("\x00\x7f"), "cause unavailable"),
        (Invalid("a" * 199), "a" * 199),
        (Invalid("a" * 200), "a" * 200),
        (Invalid("a" * 201), "a" * 200),
    ],
)
def test_preparation_cause(error: Exception, cause: str) -> None:
    with pytest.raises(Invalid) as caught, worktree.preparation_step("example"):
        raise error
    assert str(caught.value) == f"run preparation failed at example: {cause}"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_preparation_success_and_unexpected_error() -> None:
    with worktree.preparation_step("example"):
        pass
    message = "unexpected"
    with (
        pytest.raises(RuntimeError, match="unexpected"),
        worktree.preparation_step("example"),
    ):
        raise RuntimeError(message)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            "fatal: https://x-access-token:SECRET@github.com/org/repo",
            "fatal: https://[redacted]@github.com/org/repo",
        ),
        (
            "fatal: ssh://SECRET@github.com/org/repo",
            "fatal: ssh://[redacted]@github.com/org/repo",
        ),
        (
            "fatal: https://name:pass@word@github.com/org/repo",
            "fatal: https://[redacted]@github.com/org/repo",
        ),
        ("fatal: https://github.com/org/repo", "fatal: https://github.com/org/repo"),
        ("Authorization: Basic example", "[redacted]"),
        ("Authorization=Bearer example-secret", "[redacted]"),
        (
            "token=example-secret password:example-secret secret=example-secret",
            "[redacted] [redacted] [redacted]",
        ),
        ("ghp_example github_pat_example", "[redacted] [redacted]"),
        ("credential=environment-value", "credential=[redacted]"),
        ("environment-value-long environment-value", "[redacted] [redacted]"),
        ("a-environment-value environment-value", "[redacted] [redacted]"),
        (
            "fatal: " + "a" * 190 + "https://x-access-token:SECRET@github.com/org/repo",
            "fatal: " + "a" * 190 + "htt",
        ),
    ],
)
def test_preparation_redacts_before_truncation(
    monkeypatch: pytest.MonkeyPatch, detail: str, expected: str
) -> None:
    # A controlled environment also proves that empty values leave text intact.
    monkeypatch.setattr(
        worktree.os,
        "environ",
        {
            "EXAMPLE_CREDENTIAL": "environment-value",
            "EXAMPLE_LONG_CREDENTIAL": "environment-value-long",
            "EXAMPLE_PREFIXED_CREDENTIAL": "a-environment-value",
            "EXAMPLE_EMPTY": "",
        },
    )
    with pytest.raises(Invalid) as caught, worktree.preparation_step("example"):
        raise subprocess.CalledProcessError(1, "sensitive-command", stderr=detail)
    assert str(caught.value) == f"run preparation failed at example: {expected}"


def test_missing_prek_reaches_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(worktree, "git", constant(""))
    monkeypatch.setattr(worktree, "configure_mirror", constant(None))
    monkeypatch.setattr(worktree, "fetch_commit", constant("a" * 40))
    monkeypatch.setattr(worktree, "clone_run", constant(None))
    monkeypatch.setattr(worktree.host_tools.shutil, "which", constant(None))
    monkeypatch.setattr(process_group, "boot_id", constant("example-boot"))
    (tmp_path / ".worktrees" / "agent-example" / ".git").mkdir(parents=True)
    files = runfiles.RunFiles(tmp_path / "runs", "example")
    with pytest.raises(Invalid) as caught:
        process_group.prepare(
            lambda: worktree.create(tmp_path, "example", run_request()),
            files,
            time.monotonic() + 5,
            0,
        )
    assert str(caught.value) == (
        "run preparation failed at hook install: host executable prek was not found"
    )


def test_git_stderr_reaches_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worktree.host_tools, "resolve", constant("/fixed/git"))

    def output(command: list[str], **kwargs: object) -> str:
        assert kwargs["stderr"] == subprocess.PIPE
        raise subprocess.CalledProcessError(
            128,
            command,
            stderr=(
                "fatal: https://x-access-token:SECRET@github.com/org/repo\n"
                "private detail"
            ),
        )

    monkeypatch.setattr(subprocess, "check_output", output)
    with pytest.raises(Invalid) as caught, worktree.preparation_step("mirror init"):
        worktree.git(tmp_path, "init", "--bare")
    assert str(caught.value) == (
        "run preparation failed at mirror init: "
        "fatal: https://[redacted]@github.com/org/repo"
    )


def test_non_github_host_is_refused(tmp_path: Path) -> None:
    value = run_request(repo="git.example.com/tbhb-dev/example-repo")
    with pytest.raises(Invalid, match="only tbhb-dev repositories"):
        worktree.create(tmp_path, "example", value)


def test_fetch_never_receives_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "sensitive-socket")
    monkeypatch.setenv("GH_TOKEN", "sensitive-token")
    calls: list[list[str]] = []

    def output(command: list[str], **kwargs: object) -> str:
        calls.append(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["GIT_ASKPASS"] == "/bin/false"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "SSH_AUTH_SOCK" not in environment
        assert "GH_TOKEN" not in environment
        assert "credential.helper=" in command
        assert "core.askPass=" in command
        assert not any("sensitive" in value for value in command)
        if "--get-regexp" in command:
            raise subprocess.CalledProcessError(1, command)
        assert command[-2:] == ["fetch", "origin"]
        return ""

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree.git(tmp_path, "fetch", "origin") == ""
    assert len(calls) == 2
