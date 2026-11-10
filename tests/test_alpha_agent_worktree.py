"""One real git worktree per run, without changing existing local work."""

from __future__ import annotations

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


def test_build_fetches_default_and_creates_one_branch(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    (seed / "tracked").write_text("new remote content\n")
    head = commit(seed, "remote change")
    git(seed, "push", "origin", "main")
    path = worktree.create(clone, "build-1", run_request())
    assert path == clone / ".worktrees" / "agent-build-1"
    assert git(path, "branch", "--show-current") == "agent/build-1"
    assert git(path, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "HEAD") == head
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
    def output(command: list[str], *, text: bool, stderr: int) -> str:
        assert command == ["git", "-C", str(tmp_path), "status", "--porcelain"]
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
        ("fetch", "origin"),
        ("symbolic-ref", "refs/remotes/origin/HEAD"),
        ("branch", "--show-current"),
        ("status", "--porcelain", "--untracked-files=no"),
        ("merge", "--ff-only", "refs/remotes/origin/main"),
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
