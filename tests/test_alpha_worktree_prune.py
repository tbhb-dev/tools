"""Tests for `tbhbdev alpha worktree-prune`, over real temporary repositories."""

import json
import os
import subprocess  # nosec B404
from pathlib import Path
from types import SimpleNamespace

import pytest

from tbhbdev.alpha import COMMANDS
from tbhbdev.alpha import worktree_prune as wp

DAY = 86400
NOW = 2_000_000_000.0
OLD = NOW - 30 * DAY


def sh(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example@example.test")
        monkeypatch.setenv(f"GIT_{role}_DATE", "2020-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    path = tmp_path / "clone"
    path.mkdir()
    sh(path, "init", "-q", "-b", "main")
    sh(path, "config", "commit.gpgsign", "false")
    sh(path, "remote", "add", "origin", "https://github.com/example/widgets.git")
    sh(path, "commit", "-q", "--allow-empty", "-m", "base")
    sh(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.chdir(tmp_path)
    return path


def add(clone: Path, name: str, *, commit: bool = False, push: bool = False) -> Path:
    path = clone / ".worktrees" / name
    sh(clone, "worktree", "add", "-q", "-b", name, str(path), "main")
    if commit:
        sh(path, "commit", "-q", "--allow-empty", "-m", name)
    if push:
        head = sh(path, "rev-parse", "HEAD")
        sh(path, "update-ref", f"refs/remotes/origin/{name}", head)
    return path


def age(path: Path, when: float) -> None:
    os.utime(path, (when, when))
    index = Path(sh(path, "rev-parse", "--absolute-git-dir")) / "index"
    if index.exists():
        os.utime(index, (when, when))


def outcome(clone: Path, **kwargs: object) -> dict[str, tuple[str, str]]:
    results = wp.run(clone, 14, apply=bool(kwargs.get("apply")), now=NOW)
    return {r.path.name: (r.action, r.reason) for r in results}


def pr_state(
    monkeypatch: pytest.MonkeyPatch, *, value: bool | None
) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    def fake(slug: str, branch: str, head: str) -> bool | None:
        calls.append((slug, branch, head))
        return value

    monkeypatch.setattr(wp, "merged_pull_request", fake)
    return calls


@pytest.fixture(autouse=True)
def no_bazel(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []
    monkeypatch.setattr(wp, "shutdown_bazel", seen.append)
    return seen


def test_registered() -> None:
    assert COMMANDS["worktree-prune"]([], "p") in {0, 1, 2}  # reads the cwd repo


def test_merged_pr_removes(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = add(clone, "done", commit=True, push=True)
    calls = pr_state(monkeypatch, value=True)
    result = outcome(clone)
    assert result == {"done": ("would remove", "pull request merged")}
    assert path.exists()
    assert calls == [
        ("example/widgets", "done", sh(path, "rev-parse", "HEAD")),
    ]


def test_apply_removes_and_shuts_bazel(
    clone: Path, monkeypatch: pytest.MonkeyPatch, no_bazel: list[Path]
) -> None:
    path = add(clone, "done", commit=True, push=True)
    pr_state(monkeypatch, value=True)
    assert outcome(clone, apply=True) == {"done": ("removed", "removed")}
    assert not path.exists()
    assert no_bazel == [path]


def test_git_refusal_keeps(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(clone, "done", commit=True, push=True)
    pr_state(monkeypatch, value=True)
    real = wp.git

    def refuse(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        if args[:2] == ("worktree", "remove"):
            return 1, ""
        return real(*args, cwd=cwd)

    monkeypatch.setattr(wp, "git", refuse)
    assert outcome(clone, apply=True) == {"done": ("keep", "git refused the removal")}
    assert wp.main(["--apply", "--repo", str(clone)], "p") == 1


def test_open_pr_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(clone, "open", commit=True, push=True)
    pr_state(monkeypatch, value=False)
    assert outcome(clone) == {"open": ("keep", "not merged")}


def test_unpushed_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(clone, "local", commit=True)
    pr_state(monkeypatch, value=False)
    assert outcome(clone) == {"local": ("keep", "unpushed commits")}


def test_unreadable_pr_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(clone, "unknown", commit=True, push=True)
    pr_state(monkeypatch, value=None)
    assert outcome(clone) == {"unknown": ("keep", "pull request state unreadable")}


def test_unreadable_origin_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sh(clone, "remote", "remove", "origin")
    sh(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    add(clone, "x", commit=True, push=True)
    calls = pr_state(monkeypatch, value=True)
    assert outcome(clone) == {"x": ("keep", "origin unreadable")}
    assert calls == []


def test_abandoned_idle_removed(clone: Path) -> None:
    path = add(clone, "stale")
    age(path, OLD)
    assert outcome(clone) == {
        "stale": ("would remove", "no commits beyond origin/main, idle 14 days")
    }


def test_abandoned_recent_kept(clone: Path) -> None:
    path = add(clone, "fresh")
    age(path, NOW - DAY)
    assert outcome(clone) == {"fresh": ("keep", "active within 14 days")}


def test_recent_index_keeps(clone: Path) -> None:
    path = add(clone, "touched")
    age(path, OLD)
    index = Path(sh(path, "rev-parse", "--absolute-git-dir")) / "index"
    os.utime(index, (NOW - DAY, NOW - DAY))
    assert outcome(clone)["touched"] == ("keep", "active within 14 days")


def test_missing_index_still_judged(clone: Path) -> None:
    path = add(clone, "noindex")
    Path(sh(path, "rev-parse", "--absolute-git-dir"), "index").unlink()
    os.utime(path, (OLD, OLD))
    assert outcome(clone)["noindex"][0] == "would remove"


def test_dirty_kept(clone: Path) -> None:
    path = add(clone, "dirty")
    age(path, OLD)
    (path / "note.txt").write_text("work")
    assert outcome(clone) == {"dirty": ("keep", "uncommitted or untracked work")}


def test_locked_kept(clone: Path) -> None:
    path = add(clone, "locked")
    sh(clone, "worktree", "lock", str(path))
    assert outcome(clone) == {"locked": ("keep", "locked")}


def test_detached_kept(clone: Path) -> None:
    path = clone / ".worktrees" / "detached"
    sh(clone, "worktree", "add", "-q", "--detach", str(path), "main")
    assert outcome(clone) == {"detached": ("keep", "detached head")}


def test_default_branch_kept(clone: Path) -> None:
    path = clone / ".worktrees" / "trunk"
    sh(clone, "branch", "-q", "master")
    sh(clone, "worktree", "add", "-q", str(path), "master")
    assert outcome(clone) == {"trunk": ("keep", "default branch")}


def test_outside_root_kept(clone: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    sh(clone, "worktree", "add", "-q", "-b", "away", str(other), "main")
    assert outcome(clone) == {"elsewhere": ("keep", "not directly under .worktrees")}


def test_cwd_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = add(clone, "here")
    (path / "sub").mkdir()
    monkeypatch.chdir(path / "sub")
    assert outcome(clone) == {"here": ("keep", "holds the working directory")}


def test_missing_directory_kept(clone: Path) -> None:
    path = add(clone, "gone")
    for child in path.iterdir():
        child.unlink()
    path.rmdir()
    assert outcome(clone) == {"gone": ("keep", "directory missing")}


def test_main_checkout_never_listed(clone: Path) -> None:
    assert outcome(clone) == {}


def test_unreadable_list_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        wp.run(tmp_path, 14, apply=False)
    assert wp.main(["--repo", str(tmp_path)], "p") == 2


def fail_git(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    result: tuple[int, str],
) -> None:
    """Make one git subcommand return `result`; every other runs for real."""
    real = wp.git

    def fake(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        return result if args[0] == command else real(*args, cwd=cwd)

    monkeypatch.setattr(wp, "git", fake)


def set_which(monkeypatch: pytest.MonkeyPatch, *, found: bool) -> None:
    def which(name: str) -> str | None:
        return f"/bin/{name}" if found else None

    monkeypatch.setattr(wp.shutil, "which", which)


def test_unreadable_status_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(clone, "s")
    fail_git(monkeypatch, "status", (1, ""))
    assert outcome(clone) == {"s": ("keep", "git state unreadable")}


def test_unreadable_ancestry_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(clone, "a")
    fail_git(monkeypatch, "merge-base", (128, ""))
    assert outcome(clone) == {"a": ("keep", "git state unreadable")}


def test_unreadable_activity_kept(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(clone, "t")
    fail_git(monkeypatch, "log", (1, ""))
    assert outcome(clone) == {"t": ("keep", "git state unreadable")}


def test_unreadable_remote_check_kept(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(clone, "r", commit=True)
    pr_state(monkeypatch, value=False)
    fail_git(monkeypatch, "for-each-ref", (1, ""))
    assert outcome(clone) == {"r": ("keep", "git state unreadable")}


def test_short_head_kept() -> None:
    wt = wp.Worktree(Path("/x/.worktrees/a"), "abc", "a", locked=False, prunable=False)
    assert wp.keep_reason(wt, Path("/x/.worktrees"), Path("/")) == "head unreadable"


def test_parse_ignores_blank_and_flags() -> None:
    text = (
        "worktree /a\0HEAD " + "1" * 40 + "\0branch refs/heads/main\0\0\0"
        "worktree /b\0HEAD " + "2" * 40 + "\0detached\0locked why\0prunable gone\0\0"
    )
    first, second = wp.parse_worktrees(text)
    assert (first.branch, first.locked, first.prunable) == ("main", False, False)
    assert (second.branch, second.locked, second.prunable) == (None, True, True)


def test_last_activity_errors(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = add(clone, "q")
    head = sh(path, "rev-parse", "HEAD")
    assert wp.last_activity(path, head) is not None
    real = wp.git
    fail_git(monkeypatch, "rev-parse", (1, ""))
    assert wp.last_activity(path, head) is None
    monkeypatch.setattr(wp, "git", real)
    fail_git(monkeypatch, "log", (0, "nan-x"))
    assert wp.last_activity(path, head) is None


def test_last_activity_stat_error(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = add(clone, "q")
    head = sh(path, "rev-parse", "HEAD")

    def refuse(_self: Path, **_kwargs: object) -> os.stat_result:
        raise OSError

    monkeypatch.setattr(Path, "stat", refuse)
    assert wp.last_activity(path, head) is None


def test_on_a_remote_unreadable(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fail_git(monkeypatch, "for-each-ref", (1, ""))
    assert wp.on_a_remote(clone, "x") is None


def test_tree_bytes_counts_files(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"x" * 5000)
    (tmp_path / "d" / "link").symlink_to("f")
    assert wp.tree_bytes(tmp_path) >= 5000


def stub_gh(monkeypatch: pytest.MonkeyPatch, outcome_: object) -> list[list[str]]:
    """Replace subprocess.run: return `outcome_`, or raise it if an exception."""
    seen: list[list[str]] = []

    def fake(argv: list[str], **kwargs: object) -> object:
        seen.append(argv)
        assert kwargs["timeout"] == wp.GH_TIMEOUT
        if isinstance(outcome_, BaseException):
            raise outcome_
        return outcome_

    set_which(monkeypatch, found=True)
    monkeypatch.setattr(wp.subprocess, "run", fake)
    return seen


def completed(out: str, code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=out, returncode=code)


def test_pr_lookup_matches_head(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        [
            {"headRefOid": "a" * 40, "isCrossRepository": True},
            {"headRefOid": "b" * 40, "isCrossRepository": False},
            "junk",
        ]
    )
    seen = stub_gh(monkeypatch, completed(body))
    assert wp.merged_pull_request("o/r", "br", "b" * 40) is True
    assert wp.merged_pull_request("o/r", "br", "a" * 40) is False
    assert wp.merged_pull_request("o/r", "br", "c" * 40) is False
    assert seen[0][1:] == [
        "pr", "list", "--repo", "github.com/o/r", "--head", "br",
        "--state", "merged", "--json", "headRefOid,isCrossRepository",
        "--limit", "100",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "done",
    [completed("[]", 1), completed("{}"), completed("not json")],
)
def test_pr_lookup_unreadable(
    monkeypatch: pytest.MonkeyPatch, done: SimpleNamespace
) -> None:
    stub_gh(monkeypatch, done)
    assert wp.merged_pull_request("o/r", "br", "h") is None


@pytest.mark.parametrize("exc", [OSError(), subprocess.TimeoutExpired("gh", 1)])
def test_pr_lookup_errors(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    stub_gh(monkeypatch, exc)
    assert wp.merged_pull_request("o/r", "br", "h") is None


def test_pr_lookup_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    set_which(monkeypatch, found=False)
    assert wp.merged_pull_request("o/r", "br", "h") is None


def test_shutdown_bazel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.undo()
    set_which(monkeypatch, found=False)
    wp.shutdown_bazel(tmp_path)  # no bazelisk: nothing to do
    calls: list[tuple[list[str], object]] = []

    def record(argv: list[str], **kwargs: object) -> None:
        calls.append((argv, kwargs["cwd"]))
        assert kwargs["timeout"] == wp.BAZEL_TIMEOUT

    set_which(monkeypatch, found=True)
    monkeypatch.setattr(wp.subprocess, "run", record)
    wp.shutdown_bazel(tmp_path)
    assert calls == [(["/bin/bazelisk", "shutdown"], tmp_path)]

    def boom(*_a: object, **_k: object) -> None:
        raise OSError

    monkeypatch.setattr(wp.subprocess, "run", boom)
    wp.shutdown_bazel(tmp_path)


def test_report_and_exit(
    clone: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = add(clone, "done", commit=True, push=True)
    add(clone, "open", commit=True, push=True)

    def merged(_slug: str, branch: str, _head: str) -> bool:
        return branch == "done"

    monkeypatch.setattr(wp, "merged_pull_request", merged)
    monkeypatch.chdir(clone)
    assert wp.main([], "p") == 0
    out = capsys.readouterr().out
    assert f"would remove: {path}: pull request merged" in out
    assert "open: not merged" in out.replace(str(clone / ".worktrees") + "/", "")
    assert "would free" in out
    assert wp.main(["--apply"], "p") == 0
    out = capsys.readouterr().out
    assert f"removed: {path}: removed" in out
    assert "freed" in out


def test_negative_idle_days(capsys: pytest.CaptureFixture[str]) -> None:
    assert wp.main(["--max-idle-days", "-1"], "p") == 2
    assert "nonnegative" in capsys.readouterr().err
