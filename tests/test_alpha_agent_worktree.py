"""One real git worktree per run, without changing existing local work."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal, TypedDict, Unpack

import pytest
from agent_host_example import commit, git, isolate_host_git, repository, run_request

from tbhbdev.alpha.agent import worktree
from tbhbdev.alpha.agent.model import Invalid


class GitOptions(TypedDict, closed=True):
    env: dict[str, str]
    text: Literal[True]
    stderr: int
    timeout: int
    cwd: str


@pytest.fixture(autouse=True)
def scratch_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in ("CACHE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    isolate_host_git(tmp_path, monkeypatch)

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
    assert (mirror / "agent-mirror.lock").exists()
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
        **kwargs: Unpack[GitOptions],
    ) -> str:
        env = kwargs["env"]
        assert command[0] == worktree.host_tools.resolve("git")
        prefix = [
            worktree.host_tools.resolve("git"),
            *(item for value in worktree.GIT_OVERRIDES for item in ("-c", value)),
            "--git-dir",
            str(tmp_path),
            "-c",
            "protocol.allow=never",
        ]
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
        assert kwargs["cwd"] == "/"
        assert kwargs["timeout"] == 30
        assert kwargs["text"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        if command[-1] == r"^(filter|merge)\.":
            assert command == [
                *prefix,
                "config",
                "--null",
                "--name-only",
                "--get-regexp",
                r"^(filter|merge)\.",
            ]
            return "filter.example.smudge\0merge.example.driver\0"
        assert command == [
            *prefix,
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
            "status",
            "--porcelain",
        ]
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
    assert (mirror / "agent-mirror.lock").is_file()
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
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path / "untrusted-developer"))
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


@pytest.mark.parametrize("command", ["fetch", "ls-remote", "status"])
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
        base
        / "tbhbdev"
        / "agent-mirrors"
        / "github.com"
        / "example-org"
        / "example-repo.git"
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
        assert args == ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD")
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
        return "ref: refs/heads/main\tHEAD" if args[0] == "ls-remote" else commit_id

    monkeypatch.setattr(worktree, "git", run)
    request = run_request(profile="review", commit="0" * 40)
    if commit_id == "invalid":
        with pytest.raises(Invalid, match=r"^mirror did not resolve a full commit$"):
            worktree.fetch_commit(tmp_path, request)
    else:
        assert worktree.fetch_commit(tmp_path, request) == commit_id
    options = (
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--no-tags",
        "--upload-pack=git-upload-pack",
    )
    assert calls == [
        ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD"),
        ("check-ref-format", "--branch", "main"),
        (
            "fetch",
            *options,
            "--no-prune",
            "origin",
            "+refs/heads/main:refs/heads/main",
            f"+{'0' * 40}:refs/heads/tbhbdev-review",
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


def test_mirror_configuration_and_hooks_are_host_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    global_config = tmp_path / "global-config"
    global_config.write_text(f"[core]\nhooksPath = {tmp_path / 'other-hooks'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    path = worktree.create(clone, "example", run_request())
    mirror = worktree.mirror_path(tmp_path, run_request().repo)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        git(mirror, "config", "--get-all", "remote.origin.fetch")
    assert caught.value.returncode == 1
    for key, value in (
        ("gc.auto", "0"),
        ("gc.pruneExpire", "never"),
        ("core.logAllRefUpdates", "false"),
        ("maintenance.auto", "false"),
        ("fetch.recurseSubmodules", "false"),
        ("submodule.recurse", "false"),
    ):
        assert git(mirror, "config", key) == value
    assert not (mirror / "FETCH_HEAD").exists()
    assert git(path, "config", "--get", "core.hooksPath") == str(
        path / ".git" / "hooks"
    )
    for name in (
        "pre-commit",
        "pre-push",
        "prepare-commit-msg",
        "commit-msg",
        "post-commit",
    ):
        hook = path / ".git" / "hooks" / name
        assert hook.stat().st_mode & 0o777 == 0o555
        assert hook.read_text().startswith("#!/bin/sh\n")


@pytest.mark.parametrize(
    "plant", ["core.fsmonitor", "core.pager", "filter.planted.smudge"]
)
def test_host_git_never_returns_to_a_launched_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plant: str
) -> None:
    _, clone = repository(tmp_path)
    launched: set[Path] = set()
    calls: list[Path] = []
    original = worktree.git

    def checked(path: Path, *args: str) -> str:
        assert path not in launched
        assert path != clone
        calls.append(path)
        return original(path, *args)

    monkeypatch.setattr(worktree, "git", checked)
    first = worktree.create(clone, "first", run_request())
    launched.add(first)
    marker = tmp_path / "fetch-marker"
    marker.write_text("untouched\n")
    (first / ".git" / "FETCH_HEAD").symlink_to(marker)
    executed = tmp_path / "executed"
    script = tmp_path / "plant"
    script.write_text(f'#!/bin/sh\nprintf invoked > "{executed}"\n')
    script.chmod(0o700)
    git(first, "config", plant, str(script))
    second = worktree.create(clone, "second", run_request())
    assert marker.read_text() == "untouched\n"
    assert not executed.exists()
    mirror = worktree.mirror_path(tmp_path, run_request().repo)
    assert set(calls) == {mirror, first, second}


def test_old_fetch_shape_writes_through_shared_fetch_head(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    marker = tmp_path / "marker"
    marker.write_text("untouched\n")
    (clone / ".git" / "FETCH_HEAD").symlink_to(marker)
    # The old supervisor fetched in the account clone's common directory.
    # This counterfactual only writes harmless data; it executes no marker.
    worktree.git(clone, "fetch", "--upload-pack=git-upload-pack", "origin")
    assert marker.read_text() != "untouched\n"
    assert "branch 'main'" in marker.read_text()


def test_mirror_fetch_never_recurses_into_a_configured_module(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    first = worktree.create(clone, "first", run_request())
    mirror = worktree.mirror_path(tmp_path, run_request().repo)
    head = git(first, "rev-parse", "HEAD")
    git(mirror, "update-index", "--add", "--cacheinfo", f"160000,{head},x")
    module = mirror / "modules" / "x"
    module.parent.mkdir()
    git(mirror, "init", str(module))
    marker = tmp_path / "module-marker"
    script = tmp_path / "upload"
    script.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\n')
    script.chmod(0o700)
    git(module, "config", "remote.origin.url", str(tmp_path / "origin"))
    git(module, "config", "remote.origin.uploadpack", str(script))
    git(mirror, "config", "fetch.recurseSubmodules", "true")
    git(mirror, "config", "submodule.x.active", "true")
    worktree.create(clone, "second", run_request())
    assert not marker.exists()


def test_copying_clone_has_no_shared_object_inode(tmp_path: Path) -> None:
    _, clone = repository(tmp_path)
    path = worktree.create(clone, "example", run_request())
    mirror = worktree.mirror_path(tmp_path, run_request().repo)
    mirror_inodes = {
        p.stat().st_ino for p in (mirror / "objects").rglob("*") if p.is_file()
    }
    run_inodes = {
        p.stat().st_ino for p in (path / ".git" / "objects").rglob("*") if p.is_file()
    }
    assert mirror_inodes
    assert run_inodes
    assert mirror_inodes.isdisjoint(run_inodes)
    assert not (path / ".git" / "objects" / "info" / "alternates").exists()


def test_default_branch_validates_the_short_branch_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def checked(path: Path, *args: str) -> str:
        assert path == tmp_path
        calls.append(args)
        return "ref: refs/heads/topic/main\tHEAD\nabc\tHEAD"

    monkeypatch.setattr(worktree, "git", checked)
    assert worktree.default_branch(tmp_path) == "refs/heads/topic/main"
    assert calls == [
        ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD"),
        ("check-ref-format", "--branch", "topic/main"),
    ]


def test_hook_installation_is_repeatable_private_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    original = Path.mkdir
    calls: list[tuple[str, int, bool]] = []

    def mkdir(path: Path, mode: int = 0o777, *, exist_ok: bool = False) -> None:
        calls.append((str(path), mode, exist_ok))
        original(path, mode=mode, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    worktree.install_hooks(tmp_path)
    worktree.install_hooks(tmp_path)
    assert calls == [(str(tmp_path / ".git" / "hooks"), 0o700, True)] * 2
    hooks = tmp_path / ".git" / "hooks"
    assert hooks.stat().st_mode & 0o777 == 0o555
    assert {p.name for p in hooks.iterdir()} == {
        "pre-commit",
        "pre-push",
        "prepare-commit-msg",
        "prepare-commit-msg.prek",
        "commit-msg",
        "commit-msg.prek",
        "post-commit",
        "post-commit.prek",
    }
    assert all(p.stat().st_mode & 0o777 == 0o555 for p in hooks.iterdir())
    for name, verb in worktree.install.IDENTITY_HOOKS:
        assert (hooks / name).read_text() == worktree.install.SHIM.format(
            mark=worktree.install.SHIM_MARK,
            verb=verb,
            hook=name,
            origin=worktree.install.ORIGIN_GREP,
        )
    for name in (
        "pre-commit",
        "pre-push",
        *(p[0] for p in worktree.install.IDENTITY_HOOKS),
    ):
        destination = name if name in ("pre-commit", "pre-push") else f"{name}.prek"
        assert (hooks / destination).read_text() == (
            f'#!/bin/sh\nexec /example/prek hook-impl --hook-type {name} -- "$@"\n'
        )


@pytest.mark.parametrize("profile", ["build", "review"])
@pytest.mark.parametrize("xdg", [False, True, "alias", "parent-alias"])
def test_prelaunch_git_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, xdg: bool | str
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if not xdg:
        monkeypatch.delenv("XDG_STATE_HOME")
    if xdg == "alias":
        target = tmp_path / "state-target"
        target.mkdir()
        alias = tmp_path / "state-alias"
        alias.symlink_to(target, target_is_directory=True)
        monkeypatch.setenv("XDG_STATE_HOME", str(alias))
    if xdg == "parent-alias":
        state = tmp_path / "state"
        state.mkdir()
        target = tmp_path / "parent-target"
        target.mkdir()
        (state / "tbhbdev").symlink_to(target, target_is_directory=True)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".worktrees").mkdir()
    full_sha = "c" * 40
    request = run_request(
        profile=profile, commit=full_sha if profile == "review" else None
    )
    mirror = worktree.mirror_path(tmp_path, request.repo)
    workspace = clone / ".worktrees" / "agent-protocol"
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def checked(path: Path, *args: str) -> str:
        calls.append((path, args))
        if args[0] == "clone":
            (workspace / ".git").mkdir()
        if args[0] == "ls-remote":
            return "ref: refs/heads/main\tHEAD"
        return full_sha if args[0] == "rev-parse" else ""

    monkeypatch.setattr(worktree, "git", checked)

    def url(argument: str) -> str:
        assert argument == request.repo
        return str(tmp_path / "origin")

    monkeypatch.setattr(worktree.repo, "ssh_url", url)
    assert worktree.create(clone, "protocol", request) == workspace
    options = (
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--no-tags",
        "--upload-pack=git-upload-pack",
    )
    common = [
        (mirror, ("init", "--bare", "--template=")),
        (mirror, ("ls-remote", "--symref", "--end-of-options", "origin", "HEAD")),
        (mirror, ("check-ref-format", "--branch", "main")),
        (
            mirror,
            (
                "fetch",
                *options,
                "--no-prune",
                "origin",
                "+refs/heads/main:refs/heads/main",
                *(
                    (f"+{full_sha}:refs/heads/tbhbdev-review",)
                    if profile == "review"
                    else ()
                ),
            ),
        ),
    ]
    reference = "refs/heads/main" if profile == "build" else full_sha
    checkout = (
        ("checkout", "-b", "agent/protocol", full_sha, "--")
        if profile == "build"
        else ("checkout", "--detach", full_sha, "--")
    )
    options = ("clone", "--no-local", "--no-hardlinks", "--no-checkout", "--template=")
    assert calls == [
        *common,
        (
            mirror,
            ("rev-parse", "--verify", "--end-of-options", reference + "^{commit}"),
        ),
        (
            mirror,
            (
                *options,
                str(mirror),
                str(workspace),
            ),
        ),
        (workspace, ("config", "remote.origin.url", str(tmp_path / "origin"))),
        (workspace, ("config", "remote.origin.pushurl", str(tmp_path / "origin"))),
        (workspace, ("config", "core.hooksPath", str(workspace / ".git" / "hooks"))),
        (workspace, checkout),
    ]
    assert workspace.is_dir()
    assert workspace.stat().st_mode & 0o777 == 0o700
    assert (workspace / ".git" / "hooks" / "pre-commit").is_file()
    assert (workspace / ".git/config.worktree").read_text() == ""
    assert (workspace / ".git/commondir").read_text() == ".\n"
    assert list((workspace / ".git/modules").iterdir()) == []
    assert (mirror / "config").read_text() == worktree.MIRROR_CONFIG.format(
        url=str(tmp_path / "origin")
    )
    assert {p.name for p in mirror.iterdir()} == {"config", "agent-mirror.lock"}


def test_existing_mirror_config_is_replaced_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config").write_text("untrusted old settings")
    probes: list[Path] = []
    original = Path.exists

    def exists(path: Path) -> bool:
        probes.append(path)
        return original(path)

    def unexpected(*_args: object) -> str:
        pytest.fail("existing mirror config must be replaced before invoking git")

    monkeypatch.setattr(worktree, "git", unexpected)
    monkeypatch.setattr(Path, "exists", exists)
    worktree.configure_mirror(tmp_path, "git@github.com:example-org/example-repo.git")
    assert probes == [tmp_path / "config"]
    assert (tmp_path / "config").read_text() == worktree.MIRROR_CONFIG.format(
        url="git@github.com:example-org/example-repo.git"
    )


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_clone_destination_is_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    workspace = tmp_path / "run"
    if kind == "directory":
        workspace.mkdir()
    elif kind == "file":
        workspace.touch()
    else:
        workspace.symlink_to(tmp_path / "missing", target_is_directory=True)

    def unexpected(*_args: object) -> str:
        pytest.fail("git must not touch an existing destination")

    monkeypatch.setattr(worktree, "git", unexpected)
    with pytest.raises(FileExistsError):
        worktree.clone_run(tmp_path / "mirror", workspace, "example")


def test_clone_directory_mode_is_private_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, int]] = []
    original = Path.mkdir

    def mkdir(path: Path, mode: int = 0o777) -> None:
        calls.append((path, mode))
        original(path, mode=mode)

    def git(*_args: object) -> str:
        assert calls == [(tmp_path / "workspace", 0o700)]
        return ""

    def hooks(path: Path) -> None:
        assert path == tmp_path / "workspace"

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(worktree, "git", git)
    monkeypatch.setattr(worktree, "install_hooks", hooks)
    worktree.clone_run(tmp_path / "mirror", tmp_path / "workspace", "example")


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_mirror_owned_paths_never_follow_aliases_before_host_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    request = run_request()
    mirror = worktree.mirror_path(tmp_path, request.repo)
    path = mirror if depth == 0 else mirror.parents[depth - 1]
    path.parent.mkdir(parents=True)
    target = tmp_path / "untouched"
    target.mkdir()
    target.chmod(0o750)
    (target / "marker").write_text("original")
    path.symlink_to(target, target_is_directory=True)

    def unexpected(*_args: object) -> str:
        pytest.fail("host git must never enter an alias for a mirror directory")

    monkeypatch.setattr(worktree, "git", unexpected)
    with pytest.raises(
        Invalid, match=r"^host mirror paths must not be symbolic links$"
    ):
        worktree.create(clone, "example", request)
    assert {p.name for p in target.iterdir()} == {"marker"}
    assert (target / "marker").read_text() == "original"
    assert target.stat().st_mode & 0o777 == 0o750


def test_run_clone_parent_cannot_redirect_git_into_a_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (clone / ".worktrees").symlink_to(cache, target_is_directory=True)

    def unexpected(*_args: object) -> str:
        pytest.fail("host git must not enter a run container in a shared cache")

    monkeypatch.setattr(worktree, "git", unexpected)
    with pytest.raises(
        Invalid, match=r"^run clone parent must not be a symbolic link$"
    ):
        worktree.create(clone, "example", run_request())
    assert list(cache.iterdir()) == []


@pytest.mark.parametrize(
    ("host", "component"),
    [
        ("github.com", "github.com"),
        ("GitHub.com", "github.com"),
        ("git.example.com", "git.example.com"),
    ],
)
def test_mirror_path_matches_driver_contract(
    tmp_path: Path, host: str, component: str
) -> None:
    assert worktree.mirror_path(tmp_path, f"{host}/example-org/example-repo") == (
        tmp_path
        / "state"
        / "tbhbdev"
        / "agent-mirrors"
        / component
        / "example-org"
        / "example-repo.git"
    )


def test_fetch_scope_never_fetches_or_prunes_agent_namespace(tmp_path: Path) -> None:
    seed, clone = repository(tmp_path)
    head = git(seed, "rev-parse", "HEAD")
    for ref in (
        "refs/heads/a",
        "refs/heads/agent/a",
        "refs/pull/1/head",
        "refs/tags/v1",
    ):
        git(seed, "push", "origin", f"{head}:{ref}")
    worktree.create(clone, "first", run_request())
    mirror = worktree.mirror_path(tmp_path, run_request().repo)
    assert git(mirror, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/main",
    ]
    git(mirror, "update-ref", "refs/heads/agent/a", head)
    git(mirror, "update-ref", "refs/tbhbdev/default", head)
    git(mirror, "update-ref", "refs/heads/kept", head)
    for ref in ("refs/heads/a", "refs/heads/agent/a"):
        git(seed, "push", "origin", f":{ref}")
        git(seed, "push", "origin", f"{head}:{ref}/b")
    worktree.create(clone, "second", run_request())
    assert git(mirror, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/agent/a",
        "refs/heads/kept",
        "refs/heads/main",
        "refs/tbhbdev/default",
    ]


def test_new_mirror_never_discovers_parent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    git(clone, "config", "include.path", str(tmp_path / "broken-config"))
    (tmp_path / "broken-config").write_text("[broken\n")
    mirror = clone / "nested.git"
    mirror.mkdir()
    calls: list[list[str]] = []
    output = subprocess.check_output

    def checked(command: list[str], **kwargs: Unpack[GitOptions]) -> str:
        calls.append(command)
        return output(command, **kwargs)

    monkeypatch.chdir(clone)
    monkeypatch.setattr(subprocess, "check_output", checked)
    worktree.configure_mirror(mirror, str(tmp_path / "origin"))
    assert len(calls) == 1
    assert "--git-dir" in calls[0]
    assert "-C" not in calls[0]
    assert "--get-regexp" not in calls[0]
    assert (mirror / "HEAD").is_file()


def test_checkout_precedes_hook_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    install = worktree.install_hooks

    def checked(path: Path) -> None:
        assert (path / "tracked").read_text() == "initial\n"
        assert git(path, "branch", "--show-current") == "agent/ordered"
        install(path)

    monkeypatch.setattr(worktree, "install_hooks", checked)
    worktree.create(clone, "ordered", run_request())


@pytest.mark.parametrize(
    "reference", ["refs/heads/a..b", "refs/heads/a@{b", "refs/heads/a.lock"]
)
def test_remote_branch_names_pass_real_git_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    repository(tmp_path)
    mirror = tmp_path / "origin"
    original = worktree.git

    def checked(path: Path, *args: str) -> str:
        if args[0] == "ls-remote":
            return f"ref: {reference}\tHEAD"
        return original(path, *args)

    monkeypatch.setattr(worktree, "git", checked)
    with pytest.raises(subprocess.CalledProcessError):
        worktree.default_branch(mirror)


def test_real_git_calls_have_bounded_operation_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, clone = repository(tmp_path)
    git(seed, "config", "filter.example.smudge", "cat")
    output = subprocess.check_output
    calls: list[tuple[list[str], int]] = []

    def checked(command: list[str], **kwargs: Unpack[GitOptions]) -> str:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int)
        calls.append((command, timeout))
        assert "transfer.fsckObjects=true" in command
        return output(command, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", checked)
    worktree.create(clone, "timeouts", run_request())
    worktree.git(seed, "fetch", "origin")
    for command, timeout in calls:
        assert timeout == (300 if "fetch" in command or "clone" in command else 30)


def test_host_checkout_disables_a_planted_run_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    original = worktree.clone_run
    marker = tmp_path / "hook-executed"

    def planted(mirror: Path, workspace: Path, url: str) -> None:
        original(mirror, workspace, url)
        hook = workspace / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir()
        hook.write_text(f'#!/bin/sh\nprintf invoked > "{marker}"\n')
        hook.chmod(0o700)

    monkeypatch.setattr(worktree, "clone_run", planted)
    path = worktree.create(clone, "planted", run_request())
    assert (path / "tracked").read_text() == "initial\n"
    assert not marker.exists()


def test_every_git_call_has_a_fixed_host_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    output = subprocess.check_output
    calls: list[str] = []

    def checked(command: list[str], **kwargs: Unpack[GitOptions]) -> str:
        assert kwargs.get("cwd") == "/"
        calls.append(kwargs["cwd"])
        return output(command, **kwargs)

    monkeypatch.chdir(clone)
    monkeypatch.setattr(subprocess, "check_output", checked)
    worktree.create(clone, "cwd", run_request())
    assert calls


def test_git_clone_command_spells_metadata_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()

    def output(command: list[str], **_kwargs: object) -> str:
        index = command.index("--git-dir")
        assert command[index : index + 4] == [
            "--git-dir",
            str(tmp_path / ".git"),
            "--work-tree",
            str(tmp_path),
        ]
        return ""

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree.git(tmp_path, "status") == ""


@pytest.mark.parametrize("command", ["init", "fetch", "clone", "status"])
@pytest.mark.parametrize("drivers", [False, True])
def test_git_operation_timeouts_without_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, drivers: bool
) -> None:
    calls: list[list[str]] = []

    def output(argv: list[str], **kwargs: object) -> str:
        calls.append(argv)
        if "--get-regexp" in argv:
            assert kwargs["timeout"] == 30
            if drivers:
                return "filter.fixture.smudge\0"
            raise subprocess.CalledProcessError(1, argv)
        assert argv[-1] == command
        assert kwargs["timeout"] == (300 if command in ("fetch", "clone") else 30)
        return " output \n"

    monkeypatch.setattr(subprocess, "check_output", output)
    assert worktree.git(tmp_path, command) == "output"
    assert len(calls) == (1 if command == "init" else 2)


def test_git_shape_is_prepared_and_repeatable(tmp_path: Path) -> None:
    directory = tmp_path / ".git"
    directory.mkdir()
    worktree.prepare_git(tmp_path)
    assert (directory / "config.worktree").read_text() == ""
    assert (directory / "commondir").read_text() == ".\n"
    assert list((directory / "modules").iterdir()) == []
    worktree.prepare_git(tmp_path)


@pytest.mark.parametrize("name", ["config.worktree", "modules", "commondir"])
@pytest.mark.parametrize("kind", ["symlink", "broken-symlink", "content", "wrong-type"])
def test_git_shape_refuses_redirected_or_unexpected_entries(
    tmp_path: Path, name: str, kind: str
) -> None:
    directory = tmp_path / ".git"
    directory.mkdir()
    entry = directory / name
    if "symlink" in kind:
        target = tmp_path / "target"
        if kind == "symlink":
            target.write_text("untouched")
        entry.symlink_to(target)
    elif name == "modules" and kind == "content":
        entry.mkdir()
        (entry / "plant").touch()
    elif (name == "modules") == (kind == "wrong-type"):
        entry.write_text("unexpected")
    else:
        entry.mkdir()
    with pytest.raises(Invalid) as caught:
        worktree.prepare_git(tmp_path)
    assert str(caught.value) == f"unexpected git metadata: {name}"


def test_git_shape_follows_checkout_and_hooks_and_supports_real_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = repository(tmp_path)
    original_git = worktree.git
    original_shape = worktree.prepare_git
    checked: list[str] = []

    def git_before_shape(path: Path, *args: str) -> str:
        if args[0] == "checkout":
            assert not (path / ".git/config.worktree").exists()
            assert not (path / ".git/modules").exists()
            assert not (path / ".git/commondir").exists()
            checked.append("checkout")
        return original_git(path, *args)

    def shape_after_hooks(path: Path) -> None:
        assert checked == ["checkout"]
        assert (path / "tracked").read_text() == "initial\n"
        assert (path / ".git/hooks/pre-commit").is_file()
        checked.append("shape")
        original_shape(path)

    monkeypatch.setattr(worktree, "git", git_before_shape)
    monkeypatch.setattr(worktree, "prepare_git", shape_after_hooks)
    workspace = worktree.create(clone, "shape", run_request())
    assert checked == ["checkout", "shape"]
    before = git(workspace, "rev-parse", "HEAD")
    (workspace / "tracked").write_text("committed\n")
    assert commit(workspace, "shape fixture") != before
    assert git(workspace, "status", "--porcelain") == ""


def test_git_shape_writes_exact_paths_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / ".git"
    directory.mkdir()
    mkdir = Path.mkdir
    write_text = Path.write_text
    calls: list[tuple[str, str, str]] = []

    def make(path: Path) -> None:
        calls.append(("mkdir", str(path), ""))
        mkdir(path)

    def write(path: Path, text: str) -> int:
        calls.append(("write", str(path), text))
        return write_text(path, text)

    monkeypatch.setattr(Path, "mkdir", make)
    monkeypatch.setattr(Path, "write_text", write)
    worktree.prepare_git(tmp_path)
    assert calls == [
        ("write", str(directory / "config.worktree"), ""),
        ("mkdir", str(directory / "info"), ""),
        ("mkdir", str(directory / "modules"), ""),
        ("write", str(directory / "commondir"), ".\n"),
    ]


def test_an_empty_git_directory_is_no_main_checkout(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    (mount / ".git").mkdir(parents=True)
    assert not worktree.is_main_checkout(mount)
    (mount / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert worktree.is_main_checkout(mount)


def test_sandbox_placeholders_inside_git_are_no_main_checkout(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    (mount / ".git" / "hooks").mkdir(parents=True)
    (mount / ".git" / "config").write_text("")
    assert not worktree.is_main_checkout(mount)
    (mount / ".git" / "HEAD").write_text("")
    assert not worktree.is_main_checkout(mount)
    (mount / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert worktree.is_main_checkout(mount)


def test_sandbox_mount_points_above_a_write_root_are_allowed(
    checkout_paths: tuple[Path, Path],
) -> None:
    source, workspace = checkout_paths
    shared = source.parent / "scratch" / "tools"
    (shared / ".git").mkdir(parents=True)
    (shared / "run").mkdir()
    worktree.validate_writes(workspace, source, (str(shared / "run"),))
    (shared / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(Invalid, match=r"reaches main checkout"):
        worktree.validate_writes(workspace, source, (str(shared / "run"),))


@pytest.fixture
def checkout_paths(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git/HEAD").write_text("ref: refs/heads/main\n")
    workspace = source / ".worktrees" / "agent-example"
    (workspace / ".git").mkdir(parents=True)
    return source, workspace


@pytest.mark.parametrize(
    "suffix", ["", ".git", ".git/hooks", "src", ".worktrees/other"]
)
@pytest.mark.parametrize("alias", [False, True])
def test_write_roots_refuse_main_checkout_paths(
    tmp_path: Path, checkout_paths: tuple[Path, Path], suffix: str, alias: bool
) -> None:
    source, workspace = checkout_paths
    root = source / suffix
    if alias:
        link = tmp_path / "tmp-alias"
        link.symlink_to(source, target_is_directory=True)
        root = link / suffix
    with pytest.raises(Invalid, match="main checkout") as caught:
        worktree.validate_writes(workspace, source, (str(root),))
    assert (
        str(caught.value)
        == f"write root {root.resolve()} reaches main checkout {source}"
    )


@pytest.mark.parametrize("suffix", ["", ".git", ".git/hooks", "src"])
def test_workspace_cannot_be_a_main_checkout(
    checkout_paths: tuple[Path, Path], suffix: str
) -> None:
    source, _ = checkout_paths
    workspace = source / suffix
    with pytest.raises(Invalid, match="main checkout"):
        worktree.validate_writes(workspace, source, ())


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_write_roots_scan_three_levels_for_other_clones(
    tmp_path: Path, checkout_paths: tuple[Path, Path], depth: int
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    clone = root.joinpath(*[f"level-{index}" for index in range(depth)])
    (clone / ".git").mkdir(parents=True)
    (clone / ".git/HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(Invalid, match="main checkout") as caught:
        worktree.validate_writes(workspace, source, (str(root),))
    relation = "reaches" if depth == 0 else "contains"
    assert str(caught.value) == f"write root {root} {relation} main checkout {clone}"


def test_source_ancestors_are_refused_without_scanning(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    with pytest.raises(Invalid, match="main checkout") as caught:
        worktree.validate_writes(workspace, source, (str(tmp_path),))
    assert str(caught.value) == f"write root {tmp_path} contains main checkout {source}"


def test_only_this_workspace_is_allowed_under_main_checkout(
    checkout_paths: tuple[Path, Path],
) -> None:
    source, workspace = checkout_paths
    worktree.validate_writes(workspace, source, (str(workspace / "src"),))
    other = workspace.parent / "agent-other"
    (other / ".git").mkdir(parents=True)
    with pytest.raises(Invalid, match="main checkout"):
        worktree.validate_writes(workspace, source, (str(other),))


def test_scan_is_bounded_and_does_not_follow_links(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    (root / "a/b/c/d/.git").mkdir(parents=True)
    (root / "neighbor").symlink_to(source, target_is_directory=True)
    (root / "ordinary-file").touch()
    linked_git = root / "linked-git"
    linked_git.mkdir()
    (linked_git / ".git").symlink_to(source / ".git", target_is_directory=True)
    worktree.validate_writes(workspace, source, (str(root), str(tmp_path / "missing")))


def test_write_root_neighboring_a_clone_is_allowed(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = source.with_name(source.name + "-cache")
    root.mkdir()
    worktree.validate_writes(workspace, source, (str(root),))
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere\n")
    worktree.validate_writes(workspace, source, (str(linked),))


@pytest.mark.parametrize("target", [".", "../elsewhere"])
def test_main_checkout_requires_its_own_common_directory(
    tmp_path: Path, checkout_paths: tuple[Path, Path], target: str
) -> None:
    source, workspace = checkout_paths
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / ".git/commondir").write_text(target + "\n")
    (other / ".git/HEAD").write_text("ref: refs/heads/main\n")
    if target == ".":
        with pytest.raises(Invalid, match="main checkout"):
            worktree.validate_writes(workspace, source, (str(other),))
    else:
        worktree.validate_writes(workspace, source, (str(other),))


@pytest.mark.parametrize("scope", ["local", "global", "system", "include", "command"])
@pytest.mark.parametrize("writable", [False, True])
def test_hooks_path_refuses_other_locations_in_every_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, writable: bool
) -> None:
    _, workspace = repository(tmp_path)
    target = tmp_path / "other-hooks"
    if scope == "local":
        git(workspace, "config", "core.hooksPath", str(target))
    elif scope == "command":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(target))
    else:
        config = tmp_path / "hooks-config"
        config.write_text(f"[core]\n hooksPath = {target}\n")
        if scope == "include":
            git(workspace, "config", "include.path", str(config))
        else:
            monkeypatch.setenv(f"GIT_CONFIG_{scope.upper()}", str(config))
            monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    with pytest.raises(Invalid, match=r"core\.hooksPath") as caught:
        worktree.validate_hooks(workspace, (str(target.parent),) if writable else ())
    reason = f" under write root {target.parent}" if writable else ""
    assert (
        str(caught.value)
        == f"core.hooksPath {target}{reason} is not the workspace hooks directory"
    )


@pytest.mark.parametrize("value", ["/dev/null", ".git/hooks", "own", "unset"])
def test_hooks_path_allows_own_hooks_and_dev_null(tmp_path: Path, value: str) -> None:
    _, workspace = repository(tmp_path)
    if value != "unset":
        git(
            workspace,
            "config",
            "core.hooksPath",
            str(workspace / ".git/hooks") if value == "own" else value,
        )
    worktree.validate_hooks(workspace, (str(workspace),))


def test_hook_lock_covers_nested_directories_and_data(tmp_path: Path) -> None:
    (tmp_path / ".git/hooks/nested").mkdir(parents=True)
    data = tmp_path / ".git/hooks/nested/data"
    data.write_text("fixture")
    data.chmod(0o644)
    worktree.install_hooks(tmp_path)
    assert data.stat().st_mode & 0o777 == 0o444
    assert data.parent.stat().st_mode & 0o777 == 0o555
    assert data.parent.parent.stat().st_mode & 0o777 == 0o555


def test_scan_skips_git_metadata(checkout_paths: tuple[Path, Path]) -> None:
    source, workspace = checkout_paths
    (workspace / ".git/metadata/.git").mkdir(parents=True)
    worktree.validate_writes(workspace, source, ())


def test_symlinked_tmp_alias_cannot_hide_a_clone(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    temporary = tmp_path / "private/tmp"
    temporary.mkdir(parents=True)
    (temporary / "clone/.git").mkdir(parents=True)
    (temporary / "clone/.git/HEAD").write_text("ref: refs/heads/main\n")
    alias = tmp_path / "tmp"
    alias.symlink_to(temporary, target_is_directory=True)
    with pytest.raises(Invalid, match="main checkout") as caught:
        worktree.validate_writes(workspace, source, (str(alias),))
    assert str(temporary) in str(caught.value)


def test_hooks_query_uses_host_environment_and_disables_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    calls: list[list[str]] = []

    def output(command: list[str], **kwargs: object) -> str:
        assert command[:3] == [
            worktree.host_tools.resolve("git"),
            "-c",
            "core.hooksPath=/dev/null",
        ]
        assert command[3:5] == ["config", "--path"]
        assert command[5:] == ["--null", "--get-all", "core.hooksPath"]
        assert kwargs == {
            "cwd": tmp_path,
            "text": True,
            "stderr": subprocess.DEVNULL,
            "timeout": 30,
        }
        calls.append(command)
        return ".git/hooks\0/dev/null\0"

    monkeypatch.setattr(subprocess, "check_output", output)
    worktree.validate_hooks(tmp_path, ())
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["", "../hooks", "alias", "~/other-hooks"])
def test_hooks_path_rejects_relative_and_symlinked_locations(
    tmp_path: Path, value: str
) -> None:
    git(tmp_path, "init", "--template=")
    (tmp_path / "alias").symlink_to(tmp_path / "other-hooks")
    git(tmp_path, "config", "core.hooksPath", value)
    with pytest.raises(Invalid, match="under write root"):
        worktree.validate_hooks(
            tmp_path, (str(tmp_path / "neighbor"), str(tmp_path.parent))
        )


@pytest.mark.parametrize("safe_first", [False, True])
def test_hooks_path_checks_unsafe_values_even_when_overridden(
    tmp_path: Path, *, safe_first: bool
) -> None:
    git(tmp_path, "init", "--template=")
    unsafe = str(tmp_path / "bad-hooks")
    values = ("/dev/null", unsafe) if safe_first else (unsafe, "/dev/null")
    for value in values:
        git(tmp_path, "config", "--add", "core.hooksPath", value)
    with pytest.raises(Invalid, match="bad-hooks"):
        worktree.validate_hooks(tmp_path, (str(tmp_path / "neighbor"),))


def test_hooks_query_skips_an_uncreated_workspace(tmp_path: Path) -> None:
    worktree.validate_hooks(tmp_path, ())


@pytest.mark.parametrize("mode", [0o001, 0o010, 0o100])
def test_hook_lock_preserves_executable_type(tmp_path: Path, mode: int) -> None:
    (tmp_path / ".git/hooks").mkdir(parents=True)
    executable = tmp_path / ".git/hooks/extra-script"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(mode)
    worktree.install_hooks(tmp_path)
    assert executable.stat().st_mode & 0o777 == 0o555


def test_checkout_detection_reads_head_of_a_real_git_directory(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)
    (real / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    link = tmp_path / "link"
    link.mkdir()
    (link / ".git").symlink_to(real / ".git")
    assert worktree.is_main_checkout(real)
    assert not worktree.is_main_checkout(link)
    head_dir = tmp_path / "head-dir"
    (head_dir / ".git" / "HEAD").mkdir(parents=True)
    assert not worktree.is_main_checkout(head_dir)


def test_hooks_query_spells_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def directory(path: Path) -> bool:
        assert path == tmp_path / ".git"
        return True

    def output(*_args: object, **_kwargs: object) -> str:
        return "/dev/null\0"

    monkeypatch.setattr(Path, "is_dir", directory)
    monkeypatch.setattr(subprocess, "check_output", output)
    worktree.validate_hooks(tmp_path, ())


def test_empty_sandbox_mount_points_are_cleared_before_the_overlap_check(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    for name in worktree.SANDBOX_MOUNT_POINTS:
        (root / name).mkdir(parents=True)
    worktree.validate_writes(workspace, source, (str(root),))
    assert not any((root / name).exists() for name in worktree.SANDBOX_MOUNT_POINTS)


def test_mount_point_holding_a_file_stays_and_still_refuses(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    (root / ".git").mkdir(parents=True)
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (root / ".codex").mkdir()
    (root / ".codex/auth").touch()
    with pytest.raises(Invalid, match="main checkout"):
        worktree.validate_writes(workspace, source, (str(root),))
    assert (root / ".codex/auth").exists()


def test_symlinked_mount_point_is_left_alone(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    target = tmp_path / "elsewhere"
    target.mkdir()
    root = tmp_path / "writable"
    root.mkdir()
    (root / ".aws").symlink_to(target, target_is_directory=True)
    worktree.validate_writes(workspace, source, (str(root),))
    assert (root / ".aws").is_symlink()
    assert target.is_dir()


def test_mount_point_that_cannot_be_removed_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".agents").mkdir()

    def refuse(self: Path) -> None:
        raise PermissionError(self)

    monkeypatch.setattr(Path, "rmdir", refuse)
    worktree.clear_mount_points((str(tmp_path),))
    assert (tmp_path / ".agents").is_dir()
