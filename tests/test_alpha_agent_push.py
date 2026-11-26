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
    push,
    run_host,
    runfiles,
)
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

SYNTHETIC = "distinctive-synthetic-push-value-7391"
type LocalRun = tuple[run_host.Run, run_host.Prepared, Path]


@pytest.fixture(autouse=True)
def umask_022() -> Generator[None]:
    """Create the private-name list as a 0644 file whatever the host umask is."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


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
def local_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalRun:
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


@pytest.mark.parametrize("pushed_in_run", [False, True])
def test_build_records_commit_without_publishing(
    local_run: LocalRun, *, pushed_in_run: bool
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    branch = "refs/heads/agent/example"

    def work(actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack) -> int:
        rc = harness(actual, paths, stack)
        if pushed_in_run:
            git(workspace, "push", target.as_uri(), f"HEAD:{branch}")
        return rc

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, work, SYNTHETIC) == 0
    sha = git(workspace, "rev-parse", "HEAD")
    assert run.files.path("head").read_text() == sha
    assert git(target, "for-each-ref", "--format=%(objectname) %(refname)") == (
        f"{sha} {branch}" if pushed_in_run else ""
    )
    assert not run.files.path("branch").exists()
    assert not run.files.path("pr").exists()


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

    monkeypatch.setattr(push, "git_with_token", command)
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


@pytest.mark.parametrize(
    "rule", ["argv", "environment", "config", "write_roots", "output"]
)
def test_token_rules(
    rule: str,
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = fix_run
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


def test_do_not_record_uncertain_run(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    run.files.uncertain = True
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("log").exists()


def run_with_work(local_run: LocalRun, work: Callable[[], None]) -> int:
    run, prepared, _target = local_run

    def finish(*_args: object) -> int:
        work()
        return 0

    with ExitStack() as stack:
        return push.run(run, prepared, stack, finish, SYNTHETIC)


def assert_run_file(run: run_host.Run, name: str, expected: str | None) -> None:
    path = run.files.path(name)
    assert (path.read_text() if path.exists() else None) == expected


@pytest.mark.parametrize(
    ("source", "with_commit", "records_commit"),
    [
        ("topic/work", False, True),
        ("detached HEAD", False, True),
        ("refs/tags/work", False, False),
        ("refs/notes/work", False, False),
        ("stash", False, False),
        ("notes", False, False),
        ("stash", True, True),
        ("tag", True, True),
    ],
)
def test_commits_off_the_run_branch(
    local_run: LocalRun,
    source: str,
    with_commit: bool,
    records_commit: bool,
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    metadata = {
        "stash": ("stash", "push", "-u"),
        "notes": ("notes", "add", "-m", "run note"),
        "tag": ("tag", "work"),
        "refs/tags/work": ("tag", "-a", "work", "-m", "keep tagged work"),
        "refs/notes/work": ("update-ref", "refs/notes/work", "HEAD"),
    }
    checkout = ("-c", source) if source == "topic/work" else ("--detach", "HEAD")

    def work() -> None:
        if source == "stash":
            (workspace / "untracked.txt").write_text("untracked work\n")
        elif source != "notes":
            git(workspace, "switch", *checkout)
            commit(workspace, "off branch")
        if source in metadata:
            git(workspace, *metadata[source])
        if source in ("refs/tags/work", "refs/notes/work", "tag"):
            git(workspace, "switch", "agent/example")
        if source.startswith("refs/"):
            git(workspace, "pack-refs", "--all")
        if with_commit:
            commit(workspace, "new run commit")

    assert run_with_work(local_run, work) == 0
    sha = git(workspace, "rev-parse", "HEAD")
    assert git(target, "for-each-ref") == ""
    assert_run_file(run, "head", sha if records_commit else None)
    assert not run.files.path("log").exists()


@pytest.mark.parametrize("work", ["fetch only", "checkout fetched ancestor", "commit"])
def test_fetch_does_not_count_as_new_history(local_run: LocalRun, work: str) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    upstream = run.home / "upstream.git"
    git(workspace, "init", "--bare", str(upstream))
    git(workspace, "remote", "add", "origin", str(upstream))
    git(workspace, "push", "origin", "HEAD:refs/heads/fetched/heads/main")
    git(workspace, "switch", "-c", "upstream/work")
    commit(workspace, "upstream ancestor")
    ancestor = git(workspace, "rev-parse", "HEAD")
    commit(workspace, "upstream tip")
    git(workspace, "push", str(upstream), "HEAD:refs/heads/fetched/heads/main")
    git(workspace, "switch", "agent/example")
    git(workspace, "branch", "-D", "upstream/work")
    if work != "fetch only":
        git(workspace, "fetch", "origin")

    def fetch() -> None:
        git(workspace, "fetch", "origin")
        if work == "checkout fetched ancestor":
            git(workspace, "switch", "-c", "topic/fetched", ancestor)
            git(workspace, "pack-refs", "--all")
        elif work == "commit":
            commit(workspace, "new run commit")

    assert run_with_work(local_run, fetch) == 0
    sha = git(workspace, "rev-parse", "HEAD") if work == "commit" else None
    assert git(target, "for-each-ref") == ""
    assert_run_file(run, "head", sha)
    assert not run.files.path("log").exists()


@pytest.mark.parametrize(
    "tracking", ["origin/agent/example", "origin/fetched/heads/main"]
)
def test_sandbox_remote_ref_cannot_hide_a_run_commit(
    local_run: LocalRun, tracking: str
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    git(workspace, "update-ref", "refs/remotes/origin/fetched/heads/main", "HEAD")

    def work() -> None:
        commit(workspace, "agent change")
        git(workspace, "update-ref", f"refs/remotes/{tracking}", "HEAD")

    assert run_with_work(local_run, work) == 0
    assert git(target, "for-each-ref") == ""
    assert_run_file(run, "head", git(workspace, "rev-parse", "HEAD"))


@pytest.mark.parametrize(
    ("start", "descends"),
    [
        ("run branch", False),
        ("attached HEAD", False),
        ("detached HEAD", False),
        ("run branch", True),
        ("attached HEAD", True),
        ("detached HEAD", True),
        ("multiple unrelated tips", False),
    ],
)
def test_only_descendants_of_start_are_recorded(
    local_run: LocalRun, start: str, descends: bool
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
    elif start == "detached HEAD":
        git(workspace, "switch", "--detach", start_sha)
        git(workspace, "branch", "-D", "agent/example")

    multiple = start == "multiple unrelated tips"
    tips = ("topic/first", "topic/second") if multiple else ("topic/work",)
    base = start_sha if descends else common

    def work() -> None:
        for tip in tips:
            git(workspace, "switch", "-c", tip, base)
            commit(workspace, tip)
        if multiple:
            git(workspace, "branch", "topic/alias", "topic/first")
            git(workspace, "switch", "agent/example")
            commit(workspace, "publishable work")

    assert run_with_work(local_run, work) == (0 if descends else 4)
    assert git(target, "for-each-ref") == ""
    assert_run_file(
        run, "head", git(workspace, "rev-parse", "HEAD") if descends else None
    )
    if not descends:
        assert not run.files.path("head").exists()
        shas = " ".join(sorted(git(workspace, "rev-parse", tip)[:12] for tip in tips))
        assert run.files.path("log").read_text() == (
            f"commit check failed: {len(tips)} new tip(s) "
            f"outside run start ancestry: {shas}\n"
        )


def test_eligible_tips_orders_refs_and_ignores_existing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start, existing, first, last = (digit * 40 for digit in "1234")
    bases = (start,)
    first_ref, last_ref = "refs/heads/a-first", "refs/heads/z-last"
    candidates: dict[str, str]
    candidates = {last_ref: last, "refs/heads/m-existing": existing, first_ref: first}
    history = Mock(side_effect=[True, False, False, True])
    monkeypatch.setattr(push, "outside_history", history)

    found = push.eligible_tips(tmp_path, candidates, bases, start)

    assert list(found.items()) == [(first_ref, first), (last_ref, last)]
    assert [query.args for query in history.call_args_list] == [
        (tmp_path, first, bases, start),
        (tmp_path, existing, bases, start),
        (tmp_path, existing, bases),
        (tmp_path, last, bases, start),
    ]


def test_unborn_start_does_not_record_a_commit(local_run: LocalRun) -> None:
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


@pytest.mark.parametrize("start_ref", [None, START_REF])
@pytest.mark.parametrize(
    "reference",
    ["refs/remotes/origin/topic", "refs/tags/topic", "refs/notes/topic", None],
)
def test_starting_fetched_history(
    local_run: LocalRun, reference: str | None, start_ref: str | None
) -> None:
    run, prepared, _target = local_run
    run.request = replace(run.request, start_ref=start_ref)
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
        if start_ref is not None:
            git(workspace, "branch", "-D", "agent/example")
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, checkout, None) == 0
    assert_run_file(
        run, "log", f"start ref: {start_ref} at {base}\n" if start_ref else None
    )


def test_ref_snapshot_reads_packed_branches_and_ignores_locks(
    local_run: LocalRun,
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    sha = git(workspace, "rev-parse", "HEAD")
    git(workspace, "pack-refs", "--all")
    (workspace / ".git" / "refs" / "heads" / "pending.lock").write_text(sha)
    ref = "refs/heads/agent/example"
    assert push.local_refs(workspace) == ({ref: sha, "HEAD": sha}, f"ref: {ref}")


def test_history_lookup_has_a_deadline(
    local_run: LocalRun,
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
        git_options = ["-c", "core.hooksPath=/dev/null", "-C", str(directory)]
        assert command == [
            host_tools.resolve("git"),
            *git_options,
            "rev-list",
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
        "commit check failed: cannot check new commits (TimeoutExpired)\n"
    )


@pytest.mark.parametrize("history", ["single", "chain", "diverged", "unsafe ref name"])
def test_run_branch_commit_after_checkout_changes(
    local_run: LocalRun,
    monkeypatch: pytest.MonkeyPatch,
    history: str,
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    commands = Mock(wraps=subprocess.run)
    unsafe = history == "unsafe ref name"
    other = ""

    def elsewhere() -> None:
        nonlocal other
        start = git(workspace, "rev-parse", "HEAD")
        commit(workspace, "run branch commit")
        checkout = ("--detach",) if unsafe else ("-c", "topic/work")
        git(workspace, "switch", *checkout, "HEAD" if history == "chain" else start)
        if history != "single":
            commit(workspace, "other branch commit")
        other = git(workspace, "rev-parse", "HEAD")
        if unsafe:
            git(workspace, "switch", "--detach", start)
            (workspace / ".git/refs/heads/other\x1b\n").write_text(other)
        monkeypatch.setattr(subprocess, "run", commands)

    assert run_with_work(local_run, elsewhere) == 0
    last_command = commands.call_args.args[0]
    assert git(target, "for-each-ref") == ""
    run_commit = git(workspace, "rev-parse", "agent/example")
    expected = other if history == "chain" else run_commit
    assert_run_file(run, "head", expected)
    if history != "single":
        assert last_command[5:] == [
            "merge-base",
            "--independent",
            run_commit,
            other,
        ]


@pytest.mark.parametrize("new_on_run_branch", [True, False])
def test_prefer_checked_out_run_branch_over_leftover_branch(
    local_run: LocalRun, new_on_run_branch: bool
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)

    def work() -> None:
        git(workspace, "switch", "-c", "topic/leftover")
        commit(workspace, "leftover change")
        git(workspace, "switch", "agent/example")
        if new_on_run_branch:
            commit(workspace, "run change")

    assert run_with_work(local_run, work) == (0 if new_on_run_branch else 4)
    sha = git(workspace, "rev-parse", "HEAD") if new_on_run_branch else None
    assert git(target, "for-each-ref") == ""
    assert_run_file(run, "head", sha)
    if not new_on_run_branch:
        expected = "commit check failed: checked-out run branch has no new commits\n"
        assert_run_file(run, "log", expected)


@pytest.mark.parametrize(
    "error", ["", "fatal: " + SYNTHETIC + "\n ! [remote rejected] x"]
)
def test_failure_redacted(
    error: str,
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = fix_run
    monkeypatch.setattr(push, "send", constant((1, error)))
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text().splitlines()[-1] == (
        "push failed: fatal: [redacted] ! [remote rejected] x"
        if error
        else "push failed: git push failed"
    )


def test_failure_log_is_capped(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = fix_run
    monkeypatch.setattr(push, "send", constant((1, "x" * 9000)))
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert (
        run.files.path("log").read_text().endswith("push failed: " + "x" * 4000 + "\n")
    )


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
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = fix_run
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
    assert git(target, "rev-parse", START_REF) == run.files.path("head").read_text()


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
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, _target = fix_run
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC * 10000) == 0


def test_push_start_failure_is_recorded(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = fix_run

    def fail(*_args: object) -> None:
        raise OSError

    monkeypatch.setattr(push, "send", fail)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert (
        run.files.path("log").read_text().splitlines()[-1]
        == "push failed: git push unavailable"
    )


def test_push_timeout_is_labeled_as_a_timeout(
    fix_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = fix_run

    def stall(*_args: object) -> None:
        command = "git"
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(push, "send", stall)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert (
        run.files.path("log").read_text().splitlines()[-1]
        == "push failed: git timed out"
    )


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


@pytest.mark.parametrize("start_ref", [START_REF, None])
@pytest.mark.parametrize("on_run_branch", [True, False])
def test_push_preserves_harness_failure(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    start_ref: str | None,
    on_run_branch: bool,
) -> None:
    run, prepared, target = local_run
    run.request = replace(run.request, start_ref=start_ref)
    base = git(Path(prepared.paths.worktree), "rev-parse", "HEAD")

    def failed_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        if not on_run_branch:
            git(Path(paths.paths.worktree), "switch", "-c", "topic/work")
        harness(actual, paths, stack)
        return 7

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, failed_harness, SYNTHETIC) == 7
    assert git(target, "for-each-ref") == ""
    assert run.files.path("head").exists() is on_run_branch
    assert not run.files.path("branch").exists()
    expected = None
    if start_ref:
        expected = f"start ref: {start_ref} at {base}\n"
        if on_run_branch:
            expected += "push skipped: harness exited 7\n"
    assert_run_file(run, "log", expected)


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
