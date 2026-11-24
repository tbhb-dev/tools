"""Push only disposable branches, with synthetic credentials in a pipe."""

from __future__ import annotations

import os
import select
import subprocess
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
from agent_host_example import constant, host_settings, run_request

from tbhbdev.alpha.agent import (
    credentials,
    host_tools,
    open_pr,
    push,
    run_host,
    runfiles,
)
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

SYNTHETIC = "distinctive-synthetic-push-value-7391"


@pytest.fixture(autouse=True)
def umask_022() -> Generator[None]:
    """Create the private-name list as a 0644 file whatever the host umask is."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@pytest.fixture(autouse=True)
def pull_request_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(open_pr, "open_pull_request", constant(7))


@pytest.fixture(autouse=True)
def push_account(
    isolated_agent_home: None, agent_suffix_case: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del isolated_agent_home, agent_suffix_case
    original_read = runfiles.read_owned

    def read(source: Path, limit: int, label: str) -> str:
        for part in source.parts:
            if part.lower() == ".git":
                assert part == ".git"
        if source.name.lower() in ("head", "packed-refs"):
            assert source.name in ("HEAD", "packed-refs")
        expected = {
            "HEAD": (1024, "run HEAD"),
            "packed-refs": (1024 * 1024, "packed refs"),
        }
        assert (limit, label) == expected.get(source.name, (1024, "run ref"))
        return original_read(source, limit, label)

    monkeypatch.setattr(runfiles, "read_owned", read)


@pytest.fixture
def local_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[run_host.Run, run_host.Prepared, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target.git"
    git(workspace, "init", "--initial-branch=agent/example")
    git(workspace, "init", "--bare", str(target))
    git(workspace, "config", "user.name", "Example")
    git(workspace, "config", "user.email", "example@example.com")
    git(workspace, "config", "commit.gpgsign", "false")
    commit(workspace, "initial")
    paths = run_host.RunPaths(
        str(tmp_path),
        str(workspace),
        str(tmp_path / "tmp"),
        str(tmp_path / "codex"),
        str(tmp_path / "audit"),
    )
    settings = host_settings()
    run = run_host.Run(
        runfiles.RunFiles(tmp_path / "state", "example"),
        run_request(),
        settings,
        settings.hosts[0],
        tmp_path,
        tmp_path / "tmp",
        0,
        100,
    )
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )

    def destination(repository: str) -> str:
        assert repository == run.request.repo
        return target.as_uri()

    monkeypatch.setattr(push, "url", destination)
    return run, prepared, target


def git(directory: Path, *args: str, data: bytes | None = None) -> str:
    return (
        subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
            cwd=directory,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(Path.home()),
                "XDG_CONFIG_HOME": str(Path.home() / ".config"),
                "XDG_STATE_HOME": str(Path.home() / ".local/state"),
                "XDG_CACHE_HOME": str(Path.home() / ".cache"),
                "XDG_DATA_HOME": str(Path.home() / ".local/share"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            input=data,
        )
        .decode()
        .strip()
    )


def commit(directory: Path, text: str) -> None:
    git(directory, "commit", "--allow-empty", "-qm", text)


def harness(run: run_host.Run, prepared: run_host.Prepared, stack: ExitStack) -> int:
    assert isinstance(stack, ExitStack)
    commit(Path(prepared.paths.worktree), "agent change")
    run.files.write("out.final", "report")
    return 0


def test_push_success(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, target = local_run
    original = push.send
    workspace = Path(prepared.paths.worktree)
    base = git(workspace, "rev-parse", "HEAD")

    def send(
        directory: Path, destination: str, branch: str, token: str
    ) -> tuple[int, str]:
        assert directory.parent == run.files.root
        assert destination == target.as_uri()
        assert branch == "agent/example"
        assert token == SYNTHETIC
        return original(directory, destination, branch, token)

    def open_request(published: open_pr.PublishedRange, *details: object) -> int:
        assert published.workspace == workspace
        assert published.git_dir.parent == run.files.root
        assert published.before == base
        assert published.commit == push.head(workspace, "agent/example")
        assert published.commit == git(target, "rev-parse", "refs/heads/agent/example")
        assert details == (
            run.request.repo,
            "agent/example",
            SYNTHETIC,
            run.files.write,
        )
        return 7

    monkeypatch.setattr(open_pr, "open_pull_request", open_request)
    monkeypatch.setattr(push, "send", send)

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    head = git(target, "rev-parse", "refs/heads/agent/example")
    assert run.files.path("head").read_text() == head
    assert run.files.path("branch").read_text() == "agent/example"
    assert run.files.path("pr").read_text() == f"{run.request.repo}#7"


START_REF = "refs/heads/fix/topic"
DEFAULT_REF = "refs/heads/trunk"


@pytest.fixture
def fix_run(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> tuple[run_host.Run, run_host.Prepared, Path]:
    run, prepared, target = local_run
    work = Path(prepared.paths.worktree)
    git(work, "push", target.as_uri(), f"HEAD:{DEFAULT_REF}")
    git(target, "symbolic-ref", "HEAD", DEFAULT_REF)
    commit(work, "PR head")
    git(work, "push", target.as_uri(), f"HEAD:{START_REF}")
    run.request = replace(run.request, start_ref=START_REF, fetch_refs=(START_REF,))
    return run, prepared, target


def test_start_ref_push_fast_forwards_the_existing_branch_without_a_pr(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, target = fix_run
    base = git(target, "rev-parse", START_REF)
    original = push.git_with_token
    operations: list[str] = []

    def command(
        directory: Path, token: str, *args: str, capture_output: bool = False
    ) -> tuple[int, str, str]:
        assert directory.parent == run.files.root
        assert token == SYNTHETIC
        if "ls-remote" in args:
            assert args[-3:] == (target.as_uri(), "HEAD", START_REF)
            operations.append("ls-remote")
        else:
            assert args == ("push", "--", target.as_uri(), f"HEAD:{START_REF}")
            operations.append("push")
        return original(directory, token, *args, capture_output=capture_output)

    def unexpected_pr(*_args: object) -> int:
        pytest.fail("start-ref push tried to open a pull request")

    monkeypatch.setattr(push, "git_with_token", command)
    monkeypatch.setattr(open_pr, "open_pull_request", unexpected_pr)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0

    assert operations == ["ls-remote", "push"]
    tip = git(target, "rev-parse", START_REF)
    assert git(target, "rev-parse", f"{START_REF}^") == base
    assert run.files.path("head").read_text() == tip
    assert run.files.path("branch").read_text() == "fix/topic"
    assert run.files.path("log").read_text() == (
        f"start ref: {START_REF} at {base}\npushed {START_REF} at {tip}\n"
    )
    assert set(git(target, "for-each-ref", "--format=%(refname)").splitlines()) == {
        START_REF,
        DEFAULT_REF,
    }
    assert not run.files.path("pr").exists()


def test_start_ref_uses_the_run_branch_when_another_branch_shares_its_tip(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = fix_run
    work = Path(prepared.paths.worktree)

    def duplicate_tip(
        run: run_host.Run, ready: run_host.Prepared, stack: ExitStack
    ) -> int:
        status = harness(run, ready, stack)
        git(work, "branch", "aaa")
        return status

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, duplicate_tip, SYNTHETIC) == 0
    assert git(target, "rev-parse", START_REF) == git(work, "rev-parse", "aaa")


def test_start_ref_pushes_a_commit_also_reachable_from_another_fetch_ref(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = fix_run
    work = Path(prepared.paths.worktree)
    other = "refs/heads/other"
    run.request = replace(run.request, fetch_refs=(START_REF, other))
    commit(work, "fix")
    fix = git(work, "rev-parse", "HEAD")
    commit(work, "follow-up")
    git(work, "push", target.as_uri(), f"HEAD:{other}")
    git(
        work,
        "fetch",
        target.as_uri(),
        f"{other}:refs/remotes/origin/fetched/heads/other",
    )
    git(work, "reset", "--hard", f"{fix}^")

    def advance(*_args: object) -> int:
        git(work, "merge", "--ff-only", fix)
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, advance, SYNTHETIC) == 0
    assert git(target, "rev-parse", START_REF) == fix


def test_start_ref_skips_push_when_the_run_branch_is_unchanged(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, target = fix_run
    base = git(target, "rev-parse", START_REF)

    def unexpected_remote(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        pytest.fail("unchanged start ref contacted the remote")

    monkeypatch.setattr(push, "git_with_token", unexpected_remote)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, constant(0), None) == 0
    assert not run.files.path("head").exists()
    assert git(target, "rev-parse", START_REF) == base


@pytest.mark.parametrize("remote_change", ["rewound", "deleted", "default", "missing"])
def test_changed_remote_ref_leaves_the_worktree_and_pushes_nothing(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
    remote_change: str,
) -> None:
    run, prepared, target = fix_run
    base = git(target, "rev-parse", START_REF)

    def change_remote(
        run: run_host.Run, ready: run_host.Prepared, stack: ExitStack
    ) -> int:
        status = harness(run, ready, stack)
        if remote_change == "rewound":
            git(target, "update-ref", START_REF, DEFAULT_REF)
        elif remote_change == "deleted":
            git(target, "update-ref", "-d", START_REF)
        elif remote_change == "default":
            git(target, "symbolic-ref", "HEAD", START_REF)
        else:
            git(target, "symbolic-ref", "HEAD", "refs/heads/missing")
        return status

    def unexpected_push(*_args: object) -> tuple[int, str]:
        pytest.fail("push ran after the remote changed")

    monkeypatch.setattr(push, "send", unexpected_push)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, change_remote, SYNTHETIC) == 4

    reason = (
        f"remote branch {START_REF} moved from {base} or no longer exists"
        if remote_change in ("rewound", "deleted")
        else f"cannot verify {START_REF} is a non-default branch"
    )
    assert f"push failed: {reason}\n" in run.files.path("log").read_text()
    assert Path(prepared.paths.worktree).is_dir()
    assert run.files.path("head").read_text() != base
    assert not run.files.path("branch").exists()
    assert not run.files.path("pr").exists()


def test_start_ref_requires_the_fetched_starting_commit(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, _target = fix_run
    git(Path(prepared.paths.worktree), "update-ref", "-d", "refs/heads/agent/example")
    with (
        ExitStack() as stack,
        pytest.raises(Invalid, match="missing fetched starting commit"),
    ):
        push.run(run, prepared, stack, harness, SYNTHETIC)
    assert not run.files.path("out.final").exists()


@pytest.mark.parametrize("branch_state", ["off-branch", "detached", "diverged"])
def test_start_ref_requires_a_fast_forward_on_the_run_branch(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path], branch_state: str
) -> None:
    run, prepared, target = fix_run
    work = Path(prepared.paths.worktree)
    base = git(target, "rev-parse", START_REF)

    def change_branch(
        run: run_host.Run, ready: run_host.Prepared, stack: ExitStack
    ) -> int:
        if branch_state == "off-branch":
            git(work, "checkout", "-b", "other")
        if branch_state == "diverged":
            git(work, "reset", "--hard", f"{base}^")
        status = harness(run, ready, stack)
        if branch_state == "detached":
            git(work, "checkout", "--detach", base)
        return status

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, change_branch, SYNTHETIC) == 4

    expected = {
        "off-branch": "needs agent/example checked out",
        "detached": "needs agent/example checked out",
        "diverged": f"does not fast-forward {START_REF} from {base}",
    }
    assert expected[branch_state] in run.files.path("log").read_text()
    assert git(target, "rev-parse", START_REF) == base


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (None, "no push token (unavailable)"),
        (credentials.PushTokenFailure("denied"), "no push token (denied)"),
        ((1, "", ""), f"cannot read remote tip of {START_REF}"),
        ((1, SYNTHETIC), "[redacted]"),
        ((1, ""), "git push failed"),
        (subprocess.TimeoutExpired("git", 1), "git timed out"),
        (OSError(), "git push unavailable"),
    ],
)
def test_start_ref_reports_push_failures(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
    outcome: tuple[int, str]
    | tuple[int, str, str]
    | Exception
    | credentials.PushTokenFailure
    | None,
    reason: str,
) -> None:
    run, prepared, _target = fix_run
    token: str | credentials.PushTokenFailure | None = SYNTHETIC
    if outcome is None or isinstance(outcome, credentials.PushTokenFailure):
        token = outcome
    elif isinstance(outcome, tuple):
        monkeypatch.setattr(
            push, "send" if len(outcome) == 2 else "git_with_token", constant(outcome)
        )
    else:

        def fail(*_args: object) -> tuple[int, str]:
            raise outcome

        monkeypatch.setattr(push, "send", fail)

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, token) == 4
    assert f"push failed: {reason}" in run.files.path("log").read_text()
    assert not run.files.path("branch").exists()


def test_a_private_repository_name_does_not_block_the_push(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    # A list file that holds repository names no longer exists as a requirement,
    # and its names in a message, a path or a branch name are not refused.
    names = run.home / ".local/state/tbhb/private-repos.txt"
    names.parent.mkdir(parents=True)
    names.write_text("fixturerepo\n")

    def named(
        actual: run_host.Run, prepared: run_host.Prepared, stack: ExitStack
    ) -> int:
        workspace = Path(prepared.paths.worktree)
        (workspace / "fixturerepo.txt").write_text("fixturerepo\n")
        git(workspace, "add", ".")
        commit(workspace, "fixturerepo change")
        return harness(actual, prepared, stack)

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, named, SYNTHETIC) == 0
    assert git(target, "rev-parse", "refs/heads/agent/example")


@pytest.mark.parametrize(
    "rule", ["argv", "environment", "config", "write_roots", "output"]
)
def test_token_rules(
    rule: str,
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    original = cast("Callable[..., subprocess.Popen[str]]", subprocess.Popen)
    commands: list[str] = []
    environments: list[str] = []

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        commands.append(str(args))
        environments.append(str(kwargs["env"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    match rule:
        case "argv":
            values = commands
        case "environment":
            values = environments
        case "config":
            values = [p.read_text() for p in run.home.rglob("config") if p.is_file()]
        case "write_roots":
            values = [
                p.read_bytes().decode(errors="replace")
                for p in run.home.rglob("*")
                if p.is_file()
            ]
        case _:
            values = [p.read_text() for p in run.files.root.iterdir() if p.is_file()]
    assert values
    assert all(SYNTHETIC not in value for value in values)


@pytest.mark.parametrize("reason", [None, "GhAppError", "TimeoutError"])
def test_missing_push_token(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], reason: str | None
) -> None:
    run, prepared, target = local_run
    token = credentials.PushTokenFailure(reason) if reason is not None else None
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, token) == 4
    sha = git(Path(prepared.paths.worktree), "rev-parse", "HEAD")
    assert run.files.path("log").read_text() == (
        f"push skipped: no push token ({reason or 'unavailable'}) for commit {sha}\n"
    )
    assert git(target, "for-each-ref") == ""
    assert run.files.path("head").read_text() == sha
    assert not run.files.path("branch").exists()
    assert not run.files.path("pr").exists()


@pytest.mark.parametrize("rc", [0, 9])
@pytest.mark.parametrize("token", [SYNTHETIC, None])
def test_no_commit(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], rc: int, token: str | None
) -> None:
    run, prepared, target = local_run
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, constant(rc), token) == rc
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("head").exists()
    assert not run.files.path("log").exists()


@pytest.mark.parametrize("token", [SYNTHETIC, None])
def test_review_bypasses_publication(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], token: str | None
) -> None:
    run, prepared, target = local_run
    review = replace(run, request=run_request(profile="review", commit="0" * 40))
    with ExitStack() as stack:
        assert push.run(review, prepared, stack, harness, token) == 0
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("head").exists()
    assert not run.files.path("log").exists()


def test_failure(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    monkeypatch.setattr(push, "url", constant((run.home / "missing").as_uri()))
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text().startswith("push failed: ")
    assert not run.files.path("branch").exists()


def test_helper_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (SYNTHETIC + "\n").encode())
    os.close(write_fd)
    try:
        command = push.helper(read_fd).removeprefix("!")
        for rc in (0, 1):
            child = subprocess.run(
                ["/bin/sh", "-c", command + " get"],
                input="protocol=https\nhost=github.com\n\n",
                text=True,
                capture_output=True,
                pass_fds=(read_fd,),
                cwd=tmp_path,
                check=False,
            )
            assert child.returncode == rc
            assert child.stdout == (
                f"username=x-access-token\npassword={SYNTHETIC}\n\n" if rc == 0 else ""
            )
    finally:
        os.close(read_fd)


def test_github_url() -> None:
    assert push.url("example/repo") == "https://github.com/example/repo.git"
    with pytest.raises(ValueError, match=r"^push requires github\.com$"):
        push.url("example.org/example/repo")


def test_broken_credential_pipe() -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    push.feed(write_fd, SYNTHETIC)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(write_fd)


def test_do_not_push_uncertain_run(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    run.files.uncertain = True
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("log").exists()


@pytest.mark.parametrize(
    "source", ["topic/work", "detached HEAD", "refs/tags/work", "refs/notes/work"]
)
def test_commits_off_the_run_branch(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], source: str
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)

    def elsewhere(*_args: object) -> int:
        if source == "topic/work":
            git(workspace, "switch", "-c", source)
        else:
            git(workspace, "switch", "--detach", "HEAD")
        commit(workspace, "off branch")
        if source.startswith("refs/"):
            if source == "refs/tags/work":
                git(workspace, "tag", "-a", "work", "-m", "keep tagged work")
            else:
                git(workspace, "update-ref", source, "HEAD")
            git(workspace, "switch", "agent/example")
            git(workspace, "pack-refs", "--all")
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, elsewhere, SYNTHETIC) == 0
    if source.startswith("refs/"):
        assert git(target, "for-each-ref") == ""
        assert not run.files.path("head").exists()
        assert not run.files.path("branch").exists()
        assert not run.files.path("log").exists()
    else:
        sha = git(workspace, "rev-parse", "HEAD")
        assert git(target, "rev-parse", "refs/heads/agent/example") == sha
        assert run.files.path("branch").read_text() == "agent/example"
        assert run.files.path("head").read_text() == sha


@pytest.mark.parametrize("work", ["fetch only", "checkout fetched ancestor", "commit"])
def test_fetch_does_not_publish_upstream_history(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], work: str
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    upstream = run.home / "upstream.git"
    git(workspace, "init", "--bare", str(upstream))
    git(workspace, "remote", "add", "origin", str(upstream))
    git(workspace, "push", "origin", "HEAD:refs/heads/main")
    git(workspace, "switch", "-c", "upstream/work")
    commit(workspace, "upstream ancestor")
    ancestor = git(workspace, "rev-parse", "HEAD")
    commit(workspace, "upstream tip")
    git(workspace, "push", str(upstream), "HEAD:refs/heads/main")
    git(workspace, "switch", "agent/example")
    git(workspace, "branch", "-D", "upstream/work")

    def fetch(*_args: object) -> int:
        git(workspace, "fetch", "origin")
        if work == "checkout fetched ancestor":
            git(workspace, "switch", "-c", "topic/fetched", ancestor)
            git(workspace, "pack-refs", "--all")
        elif work == "commit":
            commit(workspace, "new run commit")
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, fetch, SYNTHETIC) == 0
    if work == "commit":
        sha = git(workspace, "rev-parse", "HEAD")
        assert git(target, "rev-parse", "refs/heads/agent/example") == sha
        assert run.files.path("head").read_text() == sha
    else:
        assert git(target, "for-each-ref") == ""
        assert not run.files.path("head").exists()
    assert not run.files.path("log").exists()


@pytest.mark.parametrize(
    ("ref", "with_commit"),
    [("stash", False), ("notes", False), ("stash", True), ("tag", True)],
)
def test_git_metadata_does_not_publish(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], ref: str, with_commit: bool
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)

    def write_metadata(*_args: object) -> int:
        if ref == "stash":
            (workspace / "untracked.txt").write_text("untracked work\n")
            git(workspace, "stash", "push", "-u")
        elif ref == "notes":
            git(workspace, "notes", "add", "-m", "run note")
        else:
            git(workspace, "switch", "--detach", "HEAD")
            commit(workspace, "tagged commit")
            git(workspace, "tag", "work")
            git(workspace, "switch", "agent/example")
        if with_commit:
            commit(workspace, "new run commit")
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, write_metadata, SYNTHETIC) == 0
    sha = git(workspace, "rev-parse", "HEAD")
    expected = f"{sha} refs/heads/agent/example" if with_commit else ""
    assert git(target, "for-each-ref", "--format=%(objectname) %(refname)") == expected
    assert not run.files.path("log").exists()


@pytest.mark.parametrize("start", ["run branch", "attached HEAD", "detached HEAD"])
@pytest.mark.parametrize("descends", [False, True])
def test_only_descendants_of_start_are_published(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], start: str, descends: bool
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    common = git(workspace, "rev-parse", "HEAD")
    commit(workspace, "run start")
    start_sha = git(workspace, "rev-parse", "HEAD")
    if start == "run branch":
        git(workspace, "switch", "-c", "topic/checkout", common)
        commit(workspace, "checkout tip")
    elif start == "attached HEAD":
        git(workspace, "branch", "-m", "topic/start")
    else:
        git(workspace, "switch", "--detach", start_sha)
        git(workspace, "branch", "-D", "agent/example")

    def work(*_args: object) -> int:
        git(workspace, "switch", "-c", "topic/work", start_sha if descends else common)
        commit(workspace, "run work")
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, work, SYNTHETIC) == 0
    if descends:
        assert git(target, "rev-parse", "refs/heads/agent/example") == git(
            workspace, "rev-parse", "HEAD"
        )
    else:
        assert git(target, "for-each-ref") == ""
        assert not run.files.path("head").exists()


def test_unborn_start_does_not_publish(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    git(workspace, "switch", "--orphan", "topic/work")
    git(workspace, "branch", "-D", "agent/example")
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("head").exists()


def test_existing_history_does_not_count_as_new_commits(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    git(workspace, "switch", "-c", "topic/existing")
    commit(workspace, "existing history")
    existing = git(workspace, "rev-parse", "HEAD")
    commit(workspace, "existing branch tip")
    git(workspace, "switch", "agent/example")

    def switch(*_args: object) -> int:
        git(workspace, "switch", "--detach", existing)
        refs = workspace / ".git" / "refs" / "heads"
        (refs / "stale.lock").write_text(existing)
        (refs / "invalid").write_text("invalid\n")
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, switch, None) == 0
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("head").exists()
    assert not run.files.path("log").exists()


@pytest.mark.parametrize(
    "reference",
    ["refs/remotes/origin/topic", "refs/tags/topic", "refs/notes/topic", None],
)
def test_starting_fetched_history(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], reference: str | None
) -> None:
    run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    base = git(workspace, "rev-parse", "HEAD")
    commit(workspace, "fetched history")
    fetched = git(workspace, "rev-parse", "HEAD")
    if reference is not None:
        commit(workspace, "starting ref tip")
    if reference == "refs/tags/topic":
        git(workspace, "tag", "-a", "topic", "-m", "existing tag")
    elif reference is not None:
        git(workspace, "update-ref", reference, "HEAD")
    git(workspace, "reset", "--hard", base)
    git(workspace, "pack-refs", "--all")
    if reference is None:
        git(workspace, "switch", "--detach", fetched)

    def checkout(*_args: object) -> int:
        if reference is not None:
            git(workspace, "update-ref", "-d", reference)
        git(workspace, "switch", "--detach", fetched)
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, checkout, None) == 0
    assert not run.files.path("log").exists()


def test_ref_snapshot_reads_packed_branches_and_ignores_locks(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    sha = git(workspace, "rev-parse", "HEAD")
    git(workspace, "pack-refs", "--all")
    (workspace / ".git" / "refs" / "heads" / "pending.lock").write_text(sha)
    assert push.local_refs(workspace) == {"refs/heads/agent/example": sha, "HEAD": sha}


def test_history_lookup_uses_protected_metadata_and_has_a_deadline(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    base = git(workspace, "rev-parse", "HEAD")

    def exceeded(
        command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        directory = cast("Path", options["cwd"])
        assert directory.parent == run.files.root
        assert command[:3] == [
            host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
        ]
        assert command[3:6] == ["-C", str(directory), "rev-list"]
        assert command[6:] == [
            f"--ancestry-path={base}",
            "--max-count=1",
            "--stdin",
            "--",
        ]
        assert options["env"] == {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(directory),
            "PATH": host_tools.search(),
            "XDG_CACHE_HOME": str(directory),
            "XDG_CONFIG_HOME": str(directory),
            "XDG_DATA_HOME": str(directory),
            "XDG_STATE_HOME": str(directory),
        }
        assert options["check"] is True
        assert options["timeout"] == 30
        raise subprocess.TimeoutExpired(command, 30)

    def expires(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        rc = harness(actual, paths, stack)
        monkeypatch.setattr(subprocess, "run", exceeded)
        return rc

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, expires, SYNTHETIC) == 4
    assert run.files.path("log").read_text() == (
        "push skipped: cannot check new commits (TimeoutExpired)\n"
    )


@pytest.mark.parametrize("history", ["single", "chain", "diverged"])
def test_commits_on_two_branches(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
    history: str,
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    commands = Mock(wraps=subprocess.run)

    def elsewhere(*_args: object) -> int:
        start = git(workspace, "rev-parse", "HEAD")
        commit(workspace, "run branch commit")
        git(
            workspace,
            "switch",
            "-c",
            "topic/work",
            "HEAD" if history == "chain" else start,
        )
        if history != "single":
            commit(workspace, "other branch commit")
        monkeypatch.setattr(subprocess, "run", commands)
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, elsewhere, SYNTHETIC) == (
            4 if history == "diverged" else 0
        )
    last_query = commands.call_args.args[0][5:]
    run_commit = git(workspace, "rev-parse", "agent/example")
    other = git(workspace, "rev-parse", "topic/work")
    if history != "diverged":
        assert git(target, "rev-parse", "refs/heads/agent/example") == (
            other if history == "chain" else run_commit
        )
        if history == "single":
            assert last_query[0] == "rev-list"
        return
    assert git(target, "for-each-ref") == ""
    assert last_query == ["merge-base", "--independent", run_commit, other]
    assert run.files.path("log").read_text() == (
        f"push skipped: commits {run_commit} {other} on agent/example, topic/work "
        "need one branch, agent/example\n"
    )


@pytest.mark.parametrize(
    "error", ["", "fatal: " + SYNTHETIC + "\n ! [remote rejected] x"]
)
def test_failure_redacted(
    error: str,
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    monkeypatch.setattr(push, "send", constant((1, error)))
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text() == (
        "push failed: fatal: [redacted] ! [remote rejected] x\n"
        if error
        else "push failed: git push failed\n"
    )


def test_failure_log_is_capped(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    monkeypatch.setattr(push, "send", constant((1, "x" * 9000)))
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text() == "push failed: " + "x" * 4000 + "\n"


def test_spawn_failure_closes_pipe(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run, prepared, _target = local_run
    created: list[int] = []
    original = os.pipe

    def pipe() -> tuple[int, int]:
        descriptors = original()
        created.extend(descriptors)
        return descriptors

    def fail(*_args: object, **_kwargs: object) -> None:
        message = "spawn failed"
        raise OSError(message)

    monkeypatch.setattr(os, "pipe", pipe)
    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(OSError, match="spawn failed"):
        push.send(Path(prepared.paths.worktree), "unused", "agent/example", SYNTHETIC)
    for descriptor in created:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    ("operation", "query"),
    [
        ("store", "protocol=https\nhost=github.com\n\n"),
        ("erase", "protocol=https\nhost=github.com\n\n"),
        ("get", "protocol=http\nhost=github.com\n\n"),
        ("get", "protocol=https\nhost=example.org\n\n"),
        ("get", "protocol=https\n\n"),
        ("get", "host=github.com\n\n"),
        ("get", " protocol=https\nhost=github.com\n\n"),
        ("get", "protocol=https\nhost=github.com \n\n"),
        ("get", ""),
    ],
)
def test_helper_refuses_other_destinations(
    operation: str, query: str, tmp_path: Path
) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (SYNTHETIC + "\n").encode())
    os.close(write_fd)
    try:
        command = push.helper(read_fd).removeprefix("!")
        child = subprocess.run(
            ["/bin/sh", "-c", command + " " + operation],
            input=query,
            text=True,
            capture_output=True,
            pass_fds=(read_fd,),
            cwd=tmp_path,
            check=False,
        )
        assert child.returncode == 1
        assert child.stdout == ""
        assert child.stderr == ""
        assert os.read(read_fd, 4096) == (SYNTHETIC + "\n").encode()
    finally:
        os.close(read_fd)


def test_host_push_ignores_sandbox_git_config(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    hook = workspace / "pre-push"
    marker = workspace / "hook-ran"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    hook.chmod(0o700)

    def hostile_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        rc = harness(actual, paths, stack)
        git(workspace, "config", "core.hooksPath", str(workspace))
        git(
            workspace,
            "config",
            f"url.{(run.home / 'missing').as_uri()}.insteadOf",
            target.as_uri(),
        )
        return rc

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, hostile_harness, SYNTHETIC) == 0
    assert not marker.exists()
    assert (
        git(target, "rev-parse", "refs/heads/agent/example")
        == run.files.path("head").read_text()
    )


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_head_reads_only_owned_ref_data(
    packed: bool, valid: bool, local_run: tuple[run_host.Run, run_host.Prepared, Path]
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    expected = git(workspace, "rev-parse", "HEAD")
    if packed:
        git(workspace, "pack-refs", "--all")
        if not valid:
            (workspace / ".git" / "packed-refs").write_text(
                "# pack-refs\n" + expected + " refs/heads/other\n"
            )
    elif not valid:
        (workspace / ".git" / "refs/heads/agent/example").write_text("bad\n")
    if valid:
        assert push.head(workspace, "agent/example") == expected
    else:
        message = "missing run commit" if packed else "invalid run commit"
        with pytest.raises(ValueError, match=rf"^{message}$"):
            push.head(workspace, "agent/example")


@pytest.mark.parametrize("padding", [" ", "\t"])
def test_packed_ref_rejects_leading_whitespace(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], padding: str
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    expected = git(workspace, "rev-parse", "HEAD")
    git(workspace, "pack-refs", "--all")
    (workspace / ".git" / "packed-refs").write_text(
        f"{padding}{expected} refs/heads/agent/example\n"
    )
    with pytest.raises(ValueError, match=r"^invalid run commit$"):
        push.head(workspace, "agent/example")


def test_large_token_does_not_block_unused_helper(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, _target = local_run
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC * 10000) == 0


def test_push_start_failure_is_recorded(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run

    def fail(*_args: object) -> None:
        raise OSError

    monkeypatch.setattr(push, "send", fail)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text() == "push failed: git push unavailable\n"


def test_push_timeout_is_labeled_as_a_timeout(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run

    def stall(*_args: object) -> None:
        command = "git"
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(push, "send", stall)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text() == "push failed: git timed out\n"


def test_send_limits_child_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    @contextmanager
    def child(command: list[str], **kwargs: object) -> Generator[SimpleNamespace]:
        captured.append({"command": command, **kwargs})
        descriptors = cast("tuple[int, ...]", kwargs["pass_fds"])
        duplicate = os.dup(descriptors[0])

        def communicate() -> tuple[None, str]:
            assert select.select([duplicate], [], [], 5)[0] == [duplicate]
            assert os.read(duplicate, 4096) == (SYNTHETIC + "\n").encode()
            return None, "one error\n"

        try:
            yield SimpleNamespace(returncode=4, communicate=communicate)
        finally:
            os.close(duplicate)

    monkeypatch.setattr(subprocess, "Popen", child)
    assert push.send(
        tmp_path, "https://github.com/example/repo.git", "agent/example", SYNTHETIC
    ) == (4, "one error\n")
    assert len(captured) == 1
    spec = captured[0]
    descriptors = cast("tuple[int, ...]", spec["pass_fds"])
    assert len(descriptors) == 1
    command = cast("list[str]", spec.pop("command"))
    assert command[0] == host_tools.resolve("git")
    assert command[-6:] == [
        "-C",
        str(tmp_path),
        "push",
        "--",
        "https://github.com/example/repo.git",
        "HEAD:refs/heads/agent/example",
    ]
    options = command[1:-6]
    assert options[::2] == ["-c"] * 6
    assert options[1::2][:2] == [
        "credential.helper=",
        f"credential.helper={push.helper(descriptors[0])}",
    ]
    assert set(options[1::2][2:]) == {
        "core.askPass=",
        "core.fsmonitor=false",
        "core.hooksPath=/dev/null",
        "credential.useHttpPath=false",
    }
    environment = cast("dict[str, str]", spec.pop("env"))
    assert environment == {
        "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(tmp_path),
        "PATH": host_tools.search(),
        "XDG_CACHE_HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path),
    }
    assert spec == {
        "cwd": tmp_path,
        "errors": "replace",
        "pass_fds": descriptors,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "text": True,
    }
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("token", [SYNTHETIC, None])
@pytest.mark.parametrize("on_run_branch", [True, False])
def test_push_preserves_harness_failure(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    token: str | None,
    on_run_branch: bool,
) -> None:
    run, prepared, target = local_run

    def failed_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        if not on_run_branch:
            git(Path(paths.paths.worktree), "switch", "-c", "topic/work")
        harness(actual, paths, stack)
        return 7

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, failed_harness, token) == 7
    assert git(target, "for-each-ref") == ""
    assert run.files.path("head").exists() is on_run_branch
    assert not run.files.path("branch").exists()
    if on_run_branch:
        assert run.files.path("log").read_text() == "push skipped: harness exited 7\n"


@pytest.mark.parametrize("missing", [False, True])
def test_push_preserves_harness_failure_when_git_metadata_is_unusable(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], missing: bool
) -> None:
    run, prepared, target = local_run

    def failed(_run: run_host.Run, paths: run_host.Prepared, _stack: ExitStack) -> int:
        directory = Path(paths.paths.worktree) / ".git"
        if missing:
            (directory / "HEAD").unlink()
        else:
            (directory / "refs" / "heads" / "agent" / "example").write_text("invalid\n")
        return 7

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, failed, SYNTHETIC) == 7
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("log").exists()


def test_feed_sends_one_line_and_closes() -> None:
    read_fd, write_fd = os.pipe()
    try:
        push.feed(write_fd, SYNTHETIC)
        assert os.read(read_fd, 4096) == (SYNTHETIC + "\n").encode()
        assert os.read(read_fd, 4096) == b""
    finally:
        os.close(read_fd)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [(open_pr.OpenRefusedError("no draft"), "no draft"), (OSError("x"), "OSError")],
)
def test_unopened_pull_request_fails_the_run_but_keeps_the_branch(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    reason: str,
) -> None:
    run, prepared, target = local_run

    def refuse(*_args: object) -> int:
        raise failure

    monkeypatch.setattr(open_pr, "open_pull_request", refuse)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 5
    assert run.files.path("branch").read_text() == "agent/example"
    assert git(target, "rev-parse", "refs/heads/agent/example")
    assert not run.files.path("pr").exists()
    assert run.files.path("log").read_text() == f"pr not opened: {reason}\n"
