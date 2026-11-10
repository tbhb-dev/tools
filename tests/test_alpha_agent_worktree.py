"""One real git worktree per run, without changing existing local work."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal, TypedDict, Unpack

import pytest
from agent_host_example import commit, git, repository, run_request

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
        assert kwargs["text"] is True
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
        (
            "fetch",
            *options,
            "--prune",
            "origin",
            "+refs/heads/agent/*:refs/heads/agent/*",
        ),
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
    assert git(mirror, "config", "--get-all", "remote.origin.fetch").splitlines() == [
        "+refs/heads/agent/*:refs/heads/agent/*",
    ]
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
        assert hook.stat().st_mode & 0o777 == 0o755
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


def test_hook_installation_is_repeatable_and_private(
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
    assert hooks.stat().st_mode & 0o777 == 0o700
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
    assert all(p.stat().st_mode & 0o777 == 0o755 for p in hooks.iterdir())
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
        (
            mirror,
            (
                "fetch",
                *options,
                "--prune",
                "origin",
                "+refs/heads/agent/*:refs/heads/agent/*",
            ),
        ),
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
    assert calls == [
        *common,
        (
            mirror,
            ("rev-parse", "--verify", "--end-of-options", reference + "^{commit}"),
        ),
        (
            mirror,
            (
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                "--template=",
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


def test_fetch_scope_and_agent_namespace_pruning(tmp_path: Path) -> None:
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
        "refs/heads/agent/a",
        "refs/heads/main",
    ]
    git(mirror, "update-ref", "refs/tbhbdev/default", head)
    git(mirror, "update-ref", "refs/heads/kept", head)
    for ref in ("refs/heads/a", "refs/heads/agent/a"):
        git(seed, "push", "origin", f":{ref}")
        git(seed, "push", "origin", f"{head}:{ref}/b")
    worktree.create(clone, "second", run_request())
    assert git(mirror, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/agent/a/b",
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
