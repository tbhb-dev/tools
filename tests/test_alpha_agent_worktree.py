"""One real git worktree per run, without changing existing local work."""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

import pytest
from agent_host_example import commit, git, repository, run_request

from tbhbdev.alpha.agent import worktree
from tbhbdev.alpha.agent.model import Invalid


@pytest.fixture(autouse=True)
def scratch_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_build_fetches_default_and_creates_one_branch(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    (seed / "tracked").write_text("new remote content\n")
    head = commit(seed, "remote change")
    git(seed, "push", "origin", "main")
    path = worktree.create(clone, "build-1", run_request())
    assert path == clone / ".worktrees" / "agent-build-1"
    assert git(path, "branch", "--show-current") == "agent/build-1"
    assert git(path, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "HEAD") != head
    assert (path / "tracked").read_text() == "new remote content\n"
    assert worktree.common_dir(path) == clone / ".git"
    assert (clone / ".git" / "agent-worktree.lock").exists()
    with pytest.raises(subprocess.CalledProcessError):
        worktree.create(clone, "build-1", run_request())
    assert git(clone, "worktree", "list", "--porcelain").count("worktree ") == 2


@pytest.mark.parametrize("dirty", [False, True])
def test_clone_work_is_preserved(tmp_path: Path, dirty: bool) -> None:
    seed, clone = repository(tmp_path)
    old = git(clone, "rev-parse", "HEAD")
    if dirty:
        (clone / "tracked").write_text("local edits\n")
    else:
        git(clone, "checkout", "-b", "existing")
    (seed / "tracked").write_text("remote edits\n")
    head = commit(seed, "remote change")
    git(seed, "push", "origin", "main")
    path = worktree.create(clone, "build-1", run_request())
    assert git(path, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "HEAD") == old
    assert (clone / "tracked").read_text() == (
        "local edits\n" if dirty else "initial\n"
    )


def test_review_detaches_at_resolved_commit(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    git(seed, "checkout", "-b", "topic")
    head = commit(seed, "review")
    git(seed, "push", "origin", "topic")
    path = worktree.create(
        clone, "review-1", run_request(profile="review", commit=head)
    )
    assert path == clone / ".worktrees" / "agent-review-1"
    assert git(path, "rev-parse", "HEAD") == head
    assert git(path, "branch", "--show-current") == ""
    worktree.remove(clone, "review-1", force=False)
    assert not path.exists()


def test_missing_commit_is_refused(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        worktree.create(
            clone, "missing", run_request(profile="review", commit="0" * 40)
        )
    assert not (clone / ".worktrees" / "agent-missing").exists()


def test_missing_clone_is_not_created(tmp_path: Path) -> None:
    clone = tmp_path / "missing"
    with pytest.raises(Invalid) as caught:
        worktree.create(clone, "example", run_request())
    assert str(caught.value) == f"repo clone {clone} does not exist"
    assert not clone.exists()


@pytest.mark.parametrize(
    ("unpublished", "force", "kept"),
    [(False, False, False), (True, False, True), (True, True, False)],
)
def test_removal_keeps_unpublished_work(
    tmp_path: Path, unpublished: bool, force: bool, kept: bool
) -> None:
    _, clone = repository(tmp_path)
    path = worktree.create(clone, "example", run_request())
    if unpublished:
        commit(path, "local work")
    worktree.remove(clone, "example", force=force)
    assert not path.exists()
    assert ("agent/example" in git(clone, "branch", "--list")) is kept


def test_dirty_worktree_requires_force(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    path = worktree.create(clone, "example", run_request())
    (path / "untracked").write_text("local work\n")
    with pytest.raises(subprocess.CalledProcessError):
        worktree.remove(clone, "example", force=False)
    assert path.exists()
    worktree.remove(clone, "example", force=True)
    assert not path.exists()


def test_a_pushed_branch_can_be_deleted(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    path = worktree.create(clone, "example", run_request())
    commit(path, "published work")
    git(path, "push", "origin", "agent/example")
    worktree.remove(clone, "example", force=False)
    assert not git(clone, "branch", "--list", "agent/example")


def test_git_suppresses_command_errors_and_returns_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def output(
        command: list[str],
        *,
        text: bool,
        stderr: int,
        env: dict[str, str],
        timeout: int,
    ) -> str:
        assert command[0] == worktree.host_tools.resolve("git")
        if command[-1] == r"^(filter|merge)\.":
            return ""
        assert command[-2:] == ["status", "--porcelain"]
        assert command[2 : 2 * len(worktree.GIT_OVERRIDES) + 1 : 2] == list(
            worktree.GIT_OVERRIDES
        )
        assert env == {
            key: os.environ[key]
            for key in ("HOME", "SSH_AUTH_SOCK", "LANG")
            if key in os.environ
        } | {
            "PATH": worktree.host_tools.search(),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
        }
        assert timeout == 30
        assert text is True
        assert stderr == subprocess.DEVNULL
        return " output \n"

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree.git(tmp_path, "status", "--porcelain") == "output"


@pytest.mark.parametrize("profile", ["build", "review"])
def test_worktree_git_contract_under_one_stable_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    clone = tmp_path / "clone"
    common = tmp_path / "git-dir"
    clone.mkdir()
    common.mkdir()
    held = False
    locks: list[Path] = []
    calls: list[tuple[str, ...]] = []
    real_lock = worktree.runfiles.locked

    @contextmanager
    def locked(path: Path) -> Generator[None]:
        nonlocal held
        locks.append(path)
        with real_lock(path):
            held = True
            yield
            held = False

    commit_id = "1" * 40

    def git(path: Path, *args: str) -> str:
        if args == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
            assert path == clone
            return str(common)
        assert held
        calls.append(args)
        responses: dict[tuple[str, ...], str] = {
            ("symbolic-ref", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
            ("branch", "--show-current"): "main",
            ("rev-parse", "--verify", f"{'0' * 40}^{{commit}}"): commit_id,
        }
        return responses.get(args, "")

    monkeypatch.setattr(worktree, "git", git)
    monkeypatch.setattr(worktree.runfiles, "locked", locked)
    request = run_request(
        profile=profile, commit="0" * 40 if profile == "review" else None
    )
    path = worktree.create(clone, "example", request)
    assert calls == [
        ("fetch", "--upload-pack=git-upload-pack", "origin"),
        ("symbolic-ref", "refs/remotes/origin/HEAD"),
        *(
            [
                (
                    "worktree",
                    "add",
                    "-b",
                    "agent/example",
                    str(path),
                    "refs/remotes/origin/main",
                )
            ]
            if profile == "build"
            else [
                ("rev-parse", "--verify", f"{'0' * 40}^{{commit}}"),
                ("worktree", "add", "--detach", str(path), commit_id),
            ]
        ),
    ]
    calls.clear()
    worktree.remove(clone, "example", force=False)
    assert locks == [common / "agent-worktree.lock"] * 2
    assert calls == [
        ("branch", "--show-current"),
        ("worktree", "remove", str(path)),
        ("rev-list", "main", "--not", "--remotes"),
        ("branch", "-D", "main"),
    ]


def test_host_git_never_runs_hooks_or_monitor(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    marker = tmp_path / "executed"
    script = f'#!/bin/sh\ntouch "{marker}"\n'
    for name in ("post-checkout", "monitor"):
        path = clone / ".git" / "hooks" / name
        path.write_text(script)
        path.chmod(0o700)
    git(clone, "config", "core.fsmonitor", str(clone / ".git" / "hooks" / "monitor"))
    (seed / "tracked").write_text("remote update\n")
    commit(seed, "remote update")
    git(seed, "push", "origin", "main")
    worktree.create(clone, "safe", run_request())
    assert not marker.exists()
    worktree.remove(clone, "safe", force=False)
    assert not marker.exists()


@pytest.mark.parametrize("socket", [None, "/example/signing.sock"])
def test_host_git_ignores_parent_git_and_other_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket: str | None
) -> None:
    monkeypatch.setenv("GIT_DIR", "/example/untrusted")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/example/untrusted")
    monkeypatch.setenv("EXAMPLE_PARENT", "private")
    if socket is None:
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    else:
        monkeypatch.setenv("SSH_AUTH_SOCK", socket)
    test_git_suppresses_command_errors_and_returns_text(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "plant",
    [
        "reference-transaction",
        "post-checkout",
        "fsmonitor",
        "uploadpack",
        "ext",
        "smudge",
        "credential",
        "ssh",
    ],
)
def test_each_repo_command_plant_is_inert(tmp_path: Path, plant: str) -> None:
    _, clone = repository(tmp_path)
    marker = tmp_path / "executed"
    executable = tmp_path / "plant"
    executable.write_text(f'#!/bin/sh\n: > "{marker}"\nexit 1\n')
    executable.chmod(0o700)
    if plant in ("reference-transaction", "post-checkout"):
        hook = clone / ".git" / "hooks" / plant
        hook.write_text(executable.read_text())
        hook.chmod(0o700)
    elif plant == "fsmonitor":
        git(clone, "config", "core.fsmonitor", str(executable))
    elif plant == "uploadpack":
        git(clone, "config", "remote.origin.url", (tmp_path / "origin").as_uri())
        git(clone, "config", "remote.origin.uploadpack", str(executable))
    elif plant == "ext":
        git(clone, "config", "remote.origin.url", f"ext::{executable}")
    elif plant == "smudge":
        name = "planted"
        (clone / ".git" / "info" / "attributes").write_text("* filter=planted\n")
        for field in ("smudge", "clean", "process"):
            git(clone, "config", f"filter.{name}.{field}", str(executable))
        git(clone, "config", f"filter.{name}.required", "true")
        git(clone, "config", "merge.planted.driver", str(executable))
    elif plant == "credential":
        git(clone, "config", "credential.helper", f"!{executable}")
    else:
        git(clone, "config", "core.sshCommand", str(executable))
    if plant == "ext":
        with pytest.raises(subprocess.CalledProcessError):
            worktree.create(clone, "safe", run_request())
        # Refusal leaves no checkout; restore a data URL to exercise removal too.
        git(clone, "config", "remote.origin.url", str(tmp_path / "origin"))
    worktree.create(clone, "safe", run_request())
    assert not marker.exists()
    worktree.remove(clone, "safe", force=False)
    assert not marker.exists()


def test_driver_overrides_are_complete() -> None:
    assert worktree.driver_overrides(
        "filter.example.smudge\0filter.example.required\0merge.example.driver\0merge.example.name\0other.key\0"
    ) == [
        "-c",
        "filter.example.smudge=",
        "-c",
        "filter.example.clean=",
        "-c",
        "filter.example.process=",
        "-c",
        "filter.example.required=false",
        "-c",
        "merge.example.driver=",
    ]


@pytest.mark.parametrize("code", [1, 2])
def test_config_enumeration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def output(command: list[str], **_kwargs: object) -> str:
        if "--get-regexp" in command:
            raise subprocess.CalledProcessError(code, command)
        return "complete"

    monkeypatch.setattr(subprocess, "check_output", output)
    if code == 1:
        assert worktree.git(tmp_path, "status") == "complete"
    else:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            worktree.git(tmp_path, "status")
        assert caught.value.returncode == 2


def test_filter_subsection_newlines_are_data() -> None:
    assert worktree.driver_overrides("filter.line\nbreak.smudge\0") == [
        "-c",
        "filter.line\nbreak.smudge=",
        "-c",
        "filter.line\nbreak.clean=",
        "-c",
        "filter.line\nbreak.process=",
        "-c",
        "filter.line\nbreak.required=false",
    ]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("merge.example.name", []),
        ("other.example.driver", []),
        ("merge.example.driver", ["-c", "merge.example.driver="]),
    ],
)
def test_only_merge_drivers_are_overridden(key: str, expected: list[str]) -> None:
    assert worktree.driver_overrides(key + "\0") == expected
