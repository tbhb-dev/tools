"""Preparation failures identify safe steps without exposing upstream errors."""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest
from agent_host_example import constant, preparation_failure, run_request

from tbhbdev.alpha.agent import worktree
from tbhbdev.alpha.agent.model import Invalid

PUBLIC = ("github.com/example-org/example-repo",)
URL = "https://github.com/example-org/example-repo.git"


@pytest.mark.parametrize("allowed", [(), ("other/repo",)])
def test_unlisted_repository_has_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: tuple[str, ...]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def unexpected(*_args: object) -> str:
        pytest.fail("unlisted repository must never invoke git")

    monkeypatch.setattr(worktree, "git", unexpected)
    before = list(tmp_path.rglob("*"))
    with pytest.raises(Invalid) as caught:
        worktree.create(tmp_path, "example", run_request(), allowed)
    assert str(caught.value) == (
        "repository example-org/example-repo: agent mirrors fetch only "
        "repositories listed in public_repos"
    )
    assert list(tmp_path.rglob("*")) == before


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
            '[remote "origin"]\nurl = git@github.com:example-org/example-repo.git\n'
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
    workspace = worktree.create(tmp_path, "example", request, PUBLIC)
    assert workspace == tmp_path / ".worktrees" / "agent-example"
    assert (
        (mirror, ("init", "--bare", "--template=")) in calls
        if not existing
        else (all(args[0] != "init" for _path, args in calls))
    )
    assert (workspace, ("config", "remote.origin.url", URL)) in calls
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
        ("prepare_git", "git metadata"),
    ],
)
@pytest.mark.parametrize(
    "error", [OSError, ValueError, Invalid, subprocess.CalledProcessError]
)
def test_worktree_failure_names_only_its_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    step: str,
    error: type[Exception],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
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
        worktree.create(tmp_path, "example", run_request(), PUBLIC)
    assert str(caught.value) == f"run preparation failed at {step}"
    assert "sensitive" not in str(caught.value)


def test_public_repository_requires_github(tmp_path: Path) -> None:
    value = run_request(repo="git.example.com/example-org/example-repo")
    with pytest.raises(Invalid, match=r"^agent mirrors require a GitHub repository$"):
        worktree.create(tmp_path, "example", value, (value.repo,))


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
