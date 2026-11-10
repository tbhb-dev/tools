"""One real git worktree per run, without changing existing local work."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from agent_host_example import commit, git, repository, run_request

from tbhbdev.alpha.agent import worktree
from tbhbdev.alpha.agent.model import Invalid


@pytest.fixture(autouse=True)
def scratch_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def url(_repo: str) -> str:
        return str(tmp_path / "origin")

    monkeypatch.setattr(worktree.repo, "ssh_url", url)
    resolve = worktree.host_tools.resolve

    def executable(name: str, path: str | None = None) -> str:
        return "/example/prek" if name == "prek" else resolve(name, path)

    monkeypatch.setattr(worktree.host_tools, "resolve", executable)


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
    mirror = worktree.mirror_path(tmp_path, run_request().repo)
    assert (path / ".git").is_dir()
    assert (mirror / "agent-fetch.lock").exists()
    assert not (path / ".git" / "objects" / "info" / "alternates").exists()
    assert git(path, "remote", "get-url", "--push", "origin") == str(
        tmp_path / "origin"
    )
    with pytest.raises(FileExistsError):
        worktree.create(clone, "build-1", run_request())
    assert git(clone, "worktree", "list", "--porcelain").count("worktree ") == 1


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
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
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
    seed, clone = repository(tmp_path)
    commit_id = git(seed, "rev-parse", "HEAD")
    calls: list[tuple[Path, tuple[str, ...]]] = []
    original = worktree.git

    def checked(path: Path, *args: str) -> str:
        assert path != clone
        calls.append((path, args))
        return original(path, *args)

    monkeypatch.setattr(worktree, "git", checked)
    request = run_request(
        profile=profile, commit=commit_id if profile == "review" else None
    )
    path = worktree.create(clone, "example", request)
    mirror = worktree.mirror_path(tmp_path, request.repo)
    assert all(directory in (mirror, path) for directory, _args in calls)
    assert (mirror / "agent-fetch.lock").is_file()
    assert (mirror.stat().st_mode & 0o777) == 0o700
    assert git(path, "rev-parse", "HEAD") == commit_id
    assert git(path, "config", "remote.origin.url") == str(tmp_path / "origin")
    assert git(path, "config", "remote.origin.pushurl") == str(tmp_path / "origin")
    assert (
        path / ".git" / "hooks" / "prepare-commit-msg"
    ).read_text() == worktree.install.SHIM.format(
        mark=worktree.install.SHIM_MARK,
        verb="trailer",
        hook="prepare-commit-msg",
        origin=worktree.install.ORIGIN_GREP,
    )
    assert (
        (path / ".git" / "hooks" / "pre-commit").read_text()
        == '#!/bin/sh\nexec /example/prek hook-impl --hook-type pre-commit -- "$@"\n'
    )


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
    worktree.create(clone, "safe", run_request())
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


@pytest.mark.parametrize("command", ["fetch", "status"])
def test_host_git_disables_prune_lazy_fetch_and_offline_protocols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    calls: list[list[str]] = []

    def output(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)
        assert "gc.auto=0" in argv
        assert "maintenance.auto=false" in argv
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        offline = "--get-regexp" in argv or command == "status"
        assert ("protocol.allow=never" in argv) is offline
        return ""

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree.git(tmp_path, command) == ""
    assert len(calls) == 2


@pytest.mark.parametrize("state", [None, "", "/example/state"])
def test_mirror_location_uses_the_host_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    if state is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", state)
    base = Path(state) if state else tmp_path / ".local" / "state"
    assert worktree.mirror_path(tmp_path, "example-org/example-repo") == (
        base / "tbhbdev" / "agent-mirrors" / "example-org" / "example-repo.git"
    )


@pytest.mark.parametrize(
    "listing",
    ["", "abc\tHEAD", "ref: refs/tags/main\tHEAD", "ref: refs/heads/main HEAD"],
)
def test_default_branch_requires_a_branch_symref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    def listing_only(path: Path, *args: str) -> str:
        assert path == tmp_path
        assert args == ("ls-remote", "--symref", "origin", "HEAD")
        return listing

    monkeypatch.setattr(worktree, "git", listing_only)
    with pytest.raises(Invalid, match=r"^origin HEAD does not name a branch$"):
        worktree.default_branch(tmp_path)


@pytest.mark.parametrize("commit_id", ["a" * 40, "b" * 64, "invalid"])
def test_fetch_resolves_only_a_full_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_id: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(path: Path, *args: str) -> str:
        assert path == tmp_path
        calls.append(args)
        return commit_id

    monkeypatch.setattr(worktree, "git", run)
    request = run_request(profile="review", commit="0" * 40)
    if commit_id == "invalid":
        with pytest.raises(Invalid, match=r"^mirror did not resolve a full commit$"):
            worktree.fetch_commit(tmp_path, request)
    else:
        assert worktree.fetch_commit(tmp_path, request) == commit_id
    assert calls == [
        (
            "fetch",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--upload-pack=git-upload-pack",
            "origin",
        ),
        ("rev-parse", "--verify", "--end-of-options", f"{'0' * 40}^{{commit}}"),
    ]


def test_existing_clone_symlink_is_refused_without_following_it(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    target = tmp_path / "untouched"
    target.mkdir()
    parent = clone / ".worktrees"
    parent.mkdir()
    (parent / "agent-example").symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        worktree.create(clone, "example", run_request())
    assert list(target.iterdir()) == []


def test_mirror_configuration_and_hooks_are_host_written(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    path = worktree.create(clone, "example", run_request())
    mirror = worktree.mirror_path(tmp_path, run_request().repo)
    assert git(mirror, "config", "--get-all", "remote.origin.fetch").splitlines() == [
        "+refs/heads/*:refs/heads/*",
        "+refs/pull/*/head:refs/pull/*/head",
    ]
    for key, value in (
        ("gc.auto", "0"),
        ("gc.pruneExpire", "never"),
        ("core.logAllRefUpdates", "false"),
    ):
        assert git(mirror, "config", key) == value
    assert not (mirror / "FETCH_HEAD").exists()
    for name in (
        "pre-commit",
        "pre-push",
        "prepare-commit-msg",
        "commit-msg",
        "post-commit",
    ):
        hook = path / ".git" / "hooks" / name
        assert hook.stat().st_mode & 0o777 == 0o755
        assert hook.read_text().startswith("#!/bin/sh\n")
