"""Real copying clones and git plants exercise the harness parent's environment."""

from __future__ import annotations

import dataclasses
import fnmatch
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from functools import partial
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import agent_git_plants
import pytest
from agent_git_plants import PLANTS, plant
from agent_host_example import commit, git, local_mirror_fetches, repository
from agent_live_evidence import codex_auth
from agent_live_host_probe import checked_live_policy, codex_sandbox_command
from test_alpha_agent_run_host import make_run
from test_alpha_agent_sandbox_live import report

from tbhbdev.alpha.agent import credentials, host_tools, run_host, worktree
from tbhbdev.alpha.agent.decisions import argv, policy
from tbhbdev.alpha.agent.model import Harness, Os, Policy


def real_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> tuple[run_host.Run, run_host.RunPaths]:
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run = make_run(
        tmp_path,
        harness=harness.value,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="git sandbox probe" if harness is Harness.CLAUDE else None,
    )
    run = dataclasses.replace(
        run,
        host=dataclasses.replace(
            run.host, os=Os.MACOS if sys.platform == "darwin" else Os.LINUX
        ),
    )
    parent = Path(run_host.repo.clone_path(run.home, run.request.repo)).parent
    _, clone = repository(parent)
    origin = parent / "origin"
    clone = clone.rename(parent / "example-repo")

    local_mirror_fetches(monkeypatch, origin)
    directory = tmp_path / "host-tools"
    directory.mkdir()
    prek = directory / "prek"
    prek.write_text("#!/bin/sh\nexit 0\n")
    prek.chmod(0o700)
    sandbox = dataclasses.replace(run.settings.sandbox, host_path=(str(directory),))
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, sandbox=sandbox)
    )
    with host_tools.configured(run.settings, run.host.os):
        workspace = worktree.create(
            clone, run.files.name, run.request, ("example-org/example-repo",)
        )
    return run, dataclasses.replace(
        run_host.planned_paths(run), worktree=str(workspace)
    )


@pytest.mark.parametrize(
    "name", [*PLANTS, "embedded-bare", "nested-config", "worktrees-entry"]
)
def test_git_parent_overrides_disable_real_plants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, paths = real_clone(tmp_path, monkeypatch, Harness.CODEX)
    marker, target = agent_git_plants.parent_plant(Path(paths.worktree), tmp_path, name)
    control = target / "nested" if name == "nested-config" else target
    options = ("-c", "safe.bareRepository=all") if name == "embedded-bare" else ()
    positive = subprocess.run(
        ["git", *options, "status", "--porcelain"],
        cwd=control,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert positive.returncode == 0, positive.stderr
    assert marker.read_text() == "invoked"
    marker.rename(tmp_path / "positive")
    environment = policy.environment(
        run.settings, run.host, Harness.CODEX, paths, "agent"
    )
    for arguments in (
        ("status", "--porcelain"),
        ("diff",),
        ("diff", "--cached"),
        ("log",),
        ("show",),
    ):
        result = subprocess.run(
            ["git", *arguments],
            cwd=target,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if name == "embedded-bare":
            assert result.returncode != 0
            if arguments[0] == "status":
                assert "safe.bareRepository" in result.stderr
        else:
            assert result.returncode == 0 or (
                result.returncode == 128
                and "cannot run :" in result.stderr
                and "external diff died" in result.stderr
            ), result.stderr
        assert not marker.exists(), arguments


def test_user_hook_and_run_home_write_probe_are_observable(tmp_path: Path) -> None:
    home = tmp_path / "account"
    codex_home = tmp_path / "run-codex"
    codex_home.mkdir()
    command, marker, written = agent_git_plants.hook_write_probe(
        home, codex_home, tmp_path
    )
    hooks = json.loads((home / ".codex" / "hooks.json").read_text())
    hook = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    subprocess.run(["/bin/sh", "-c", hook], check=True, timeout=5)
    assert marker.read_text() == "invoked"
    subprocess.run(["/bin/sh", "-c", command], check=True, timeout=5)
    assert json.loads(written.read_text()) == hooks


@pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="set TBHBDEV_SANDBOX_LIVE=1 for real harness git probes",
)
@pytest.mark.parametrize("temp_write", [False, True])
@pytest.mark.parametrize("name", PLANTS)
@pytest.mark.parametrize("harness", [Harness.CODEX])
def test_live_harness_parent_ignores_each_git_plant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    harness: Harness,
    temp_write: bool,
) -> None:
    account_home = Path.home()
    installed = shutil.which(harness.value)
    assert installed is not None, f"{harness}: harness binary not installed"
    run, paths = real_clone(tmp_path, monkeypatch, harness)
    harness_write = tmp_path / "harness-write"
    harness_write.mkdir()
    sandbox = dataclasses.replace(
        run.settings.sandbox,
        host_path=(str(Path(installed).parent),),
        write_roots=(str(harness_write),) if temp_write else (),
        denied_home_paths=(
            *policy.denied_paths(run.settings, run.host, str(account_home), paths.uid),
            *(
                str(account_home / name)
                for name in (".codex", ".ssh", ".claude", ".claude.json")
            ),
        ),
    )
    run = dataclasses.replace(
        run, settings=dataclasses.replace(run.settings, sandbox=sandbox)
    )
    control = Path(paths.worktree) / "shell-control"
    command_text = f"printf invoked > {shlex.quote(str(control))}"
    with codex_auth(run, paths, account_home, tmp_path):
        attempt, user_marker, written = agent_git_plants.hook_write_probe(
            run.home, Path(paths.codex_home), tmp_path
        )
        command_text = attempt + " || :; " + command_text
        if name == "gitdir":
            baseline = execute_sandbox_git_probe(run, paths, command_text)
            assert baseline.returncode == 0, baseline.stderr
            assert control.read_text() == "invoked"
            control.rename(tmp_path / "unplanted-shell-control")
        marker, target = plant(Path(paths.worktree), tmp_path, name)
        git(target, "status", "--porcelain")
        assert marker.exists(), "plain git status did not fire the positive control"
        marker.rename(tmp_path / "positive")
        executed = execute_native_git_probe(run, paths, command_text)
        assert not marker.exists(), f"{harness} executed the {name} plant"
        if control.is_file():
            assert executed, "no shell tool execution event"
            assert control.read_text() == "invoked"
        else:
            assert run.host.os is Os.LINUX
            assert name == "gitdir"
            refused = execute_sandbox_git_probe(run, paths, command_text)
            assert_gitdir_startup_refused(refused, Path(paths.worktree))
            assert not control.exists(), (
                "the refused sandbox executed the shell control"
            )
            report("codex gitdir: sandbox startup refused; unplanted control observed")
        assert not marker.exists(), f"{harness} executed the {name} plant"
        assert not user_marker.exists(), "Codex loaded the run home's user hook"
        assert not written.exists(), "the shell tool wrote hooks into CODEX_HOME"


def execute_native_git_probe(
    run: run_host.Run, paths: run_host.RunPaths, command_text: str
) -> bool:
    run.files.write(
        "prompt", f"Run this exact shell command once, then finish: {command_text}"
    )
    value, executable = run_host.checked_policy(run, paths)
    prepared = run_host.Prepared(
        paths=paths,
        delivery=credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        auth_before=None,
    )
    command = run_host.harness_command(run, prepared, value, executable)
    if run.request.harness is Harness.CODEX:
        model_index = command.index("-m")
        command = (
            *command[:model_index],
            *command[model_index + 2 : -1],
            "--json",
            command[-1],
        )
    result = subprocess.run(
        command,
        input=run.files.path("prompt").read_text(),
        cwd=paths.worktree,
        env=value.env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"{run.request.harness}: no successful harness evidence"
    )
    if run.request.harness is Harness.CODEX:
        events = [json.loads(line) for line in result.stdout.splitlines()]
        return any(
            event.get("item", {}).get("type") == "command_execution" for event in events
        )
    return True


def execute_sandbox_git_probe(
    run: run_host.Run, paths: run_host.RunPaths, command_text: str
) -> subprocess.CompletedProcess[str]:
    run.files.write("prompt", command_text)
    value, executable = checked_live_policy(run, paths)
    return subprocess.run(
        codex_sandbox_command(
            run, paths, value, executable, "/bin/sh", "-c", command_text
        ),
        cwd=paths.worktree,
        env=value.env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def assert_gitdir_startup_refused(
    result: subprocess.CompletedProcess[str], workspace: Path
) -> None:
    # Codex rust-v0.164.0, linux-sandbox/src/bwrap.rs:642-648, 689-693,
    # 1245-1250, 1283-1292: a writable .git file cannot hold synthetic child
    # mount targets. Require the direct runtime error, never model prose.
    assert result.returncode != 0, result.stdout
    assert "bwrap: " in result.stderr, result.stderr
    assert "Not a directory" in result.stderr, result.stderr
    assert str(workspace / ".git") in result.stderr, result.stderr


def test_tool_git_environment_keeps_scratch_commits_out_of_the_run_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, paths = real_clone(tmp_path, monkeypatch, Harness.CODEX)
    workspace = Path(paths.worktree)
    before = git(workspace, "rev-parse", "HEAD")
    value = policy.build(
        run.settings,
        run.host,
        harness=Harness.CODEX,
        profile=run.request.profile,
        account=run.request.account,
        unsandboxed=False,
        paths=paths,
    )
    command = argv.harness(
        run.request, value, worktree=paths.worktree, brief_file="fixture"
    )
    overrides = "\n".join(
        value for flag, value in itertools.pairwise(command) if flag == "-c"
    )
    shell = tomllib.loads(overrides)["shell_environment_policy"]
    inherited = value.env | {
        "GIT_DIR": str(workspace / ".git"),
        "GIT_COMMON_DIR": str(workspace / ".git"),
        "GIT_WORK_TREE": str(workspace),
        "GIT_INDEX_FILE": str(workspace / ".git/index"),
        "GIT_CONFIG_KEY_20": "core.fsmonitor",
        "GIT_CONFIG_VALUE_20": "/example/plant",
        "GIT_CONFIG_PARAMETERS": "fixture poison",
    }
    environment = {
        name: value
        for name, value in inherited.items()
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in shell["exclude"])
    } | shell["set"]
    assert {
        name: value for name, value in environment.items() if name.startswith("GIT_")
    } == {"GIT_CONFIG_COUNT": "0"}
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    git(scratch, "init", "--template=", ".", env=environment)
    commit(scratch, "scratch", env=environment)
    assert git(scratch, "log", "-1", "--format=%s") == "scratch"
    assert git(workspace, "rev-parse", "HEAD") == before


def fake_sandbox_git_probe(
    evidence: str,
    observed: list[str],
    _run: run_host.Run,
    paths: run_host.RunPaths,
    command_text: str,
) -> subprocess.CompletedProcess[str]:
    workspace = Path(paths.worktree)
    assert "hooks.json" in command_text
    if (workspace / ".git").is_dir():
        observed.append("baseline")
        if evidence == "baseline-failed":
            return subprocess.CompletedProcess([], 1, stderr="fixture failure")
        (workspace / "shell-control").write_text("invoked")
        return subprocess.CompletedProcess([], 0, stderr="")
    observed.append("refusal")
    if evidence == "direct-wrote":
        (workspace / "shell-control").write_text("invoked")
    if evidence == "direct-plant":
        (workspace.parent / "git-marker").write_text("invoked")
    target = workspace.parent / "other" if evidence == "unrelated-path" else workspace
    metadata = target / (".git" if evidence == "file-target" else ".git/config")
    diagnostic = f"bwrap: Can't mkdir parents for {metadata}: Not a directory"
    if evidence == "model-claim":
        diagnostic = "The sandbox failed to initialize, so the command did not execute"
    elif evidence == "wrong-error":
        diagnostic = diagnostic.replace("Not a directory", "Permission denied")
    return subprocess.CompletedProcess(
        [], int(evidence != "direct-succeeded"), stderr=diagnostic
    )


def fake_native_git_probe(
    evidence: str,
    observed: list[str],
    run: run_host.Run,
    paths: run_host.RunPaths,
    command_text: str,
) -> bool:
    assert "hooks.json" in command_text
    observed.append("native")
    if evidence in {"executed", "no-event"}:
        (Path(paths.worktree) / "shell-control").write_text("invoked")
    if evidence == "parent-plant":
        (Path(paths.worktree).parent / "git-marker").write_text("invoked")
    if evidence == "user-hook":
        (Path(paths.worktree).parent / "user-hook-marker").write_text("invoked")
    assert run.request.harness is Harness.CODEX
    return evidence == "executed"


@pytest.mark.parametrize("temp_write", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        ("gitdir", Os.LINUX, "refused", True),
        ("gitdir", Os.LINUX, "file-target", True),
        ("gitdir", Os.LINUX, "executed", True),
        ("gitdir", Os.MACOS, "executed", True),
        ("config", Os.LINUX, "executed", True),
        ("config", Os.LINUX, "refused", False),
        ("gitdir", Os.MACOS, "refused", False),
        ("gitdir", Os.LINUX, "baseline-failed", False),
        ("gitdir", Os.LINUX, "parent-plant", False),
        ("gitdir", Os.LINUX, "user-hook", False),
        ("gitdir", Os.LINUX, "model-claim", False),
        ("gitdir", Os.LINUX, "unrelated-path", False),
        ("gitdir", Os.LINUX, "wrong-error", False),
        ("gitdir", Os.LINUX, "direct-succeeded", False),
        ("gitdir", Os.LINUX, "direct-wrote", False),
        ("gitdir", Os.LINUX, "direct-plant", False),
        ("gitdir", Os.LINUX, "no-event", False),
    ],
)
def test_gitdir_refusal_needs_direct_evidence_and_unplanted_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, Os, str, bool],
    *,
    temp_write: bool,
) -> None:
    name, os_name, evidence, passes = case
    module = sys.modules[__name__]
    which = shutil.which
    observed: list[str] = []

    def installed(command: str, path: str | None = None) -> str | None:
        return sys.executable if command == "codex" else which(command, path=path)

    def no_auth(_source: Path, _destination: Path) -> None:
        return None

    def scratch_home() -> Path:
        return tmp_path / "account"

    def local_clone(
        root: Path, _patch: pytest.MonkeyPatch, harness: Harness
    ) -> tuple[run_host.Run, run_host.RunPaths]:
        run = make_run(root, harness=harness.value)
        workspace = root / "workspace"
        (workspace / ".git").mkdir(parents=True)
        return (
            dataclasses.replace(run, host=dataclasses.replace(run.host, os=os_name)),
            dataclasses.replace(run_host.planned_paths(run), worktree=str(workspace)),
        )

    def local_plant(workspace: Path, root: Path, kind: str) -> tuple[Path, Path]:
        if kind == "gitdir":
            (workspace / ".git").rename(root / "common.git")
            (workspace / ".git").write_text(f"gitdir: {root / 'common.git'}\n")
        return root / "git-marker", workspace

    def plain_status(_workspace: Path, *arguments: str) -> str:
        assert arguments == ("status", "--porcelain")
        (tmp_path / "git-marker").write_text("invoked")
        return ""

    monkeypatch.setattr(Path, "home", scratch_home)
    monkeypatch.setattr(shutil, "which", installed)
    monkeypatch.setattr(credentials, "auth_copy", no_auth)
    monkeypatch.setattr(module, "real_clone", local_clone)
    monkeypatch.setattr(module, "plant", local_plant)
    monkeypatch.setattr(module, "git", plain_status)
    monkeypatch.setattr(
        module,
        "execute_native_git_probe",
        partial(fake_native_git_probe, evidence, observed),
    )
    monkeypatch.setattr(
        module,
        "execute_sandbox_git_probe",
        partial(fake_sandbox_git_probe, evidence, observed),
    )
    if passes:
        test_live_harness_parent_ignores_each_git_plant(
            tmp_path, monkeypatch, name, Harness.CODEX, temp_write
        )
        assert observed == (
            ["baseline", "native", "refusal"]
            if evidence in {"refused", "file-target"}
            else ["baseline", "native"]
            if name == "gitdir"
            else ["native"]
        )
    else:
        with pytest.raises(AssertionError):
            test_live_harness_parent_ignores_each_git_plant(
                tmp_path, monkeypatch, name, Harness.CODEX, temp_write
            )


@pytest.mark.parametrize("mode", ["native", "sandbox"])
@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("execution_event", [False, True])
def test_git_probe_commands_require_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    exit_code: int,
    *,
    execution_event: bool,
) -> None:
    run = make_run(tmp_path)
    paths = run_host.planned_paths(run)
    value = policy.build(
        run.settings,
        run.host,
        harness=Harness.CODEX,
        profile=run.request.profile,
        account=run.request.account,
        unsandboxed=False,
        paths=paths,
    )
    command_text = "fixture command"

    def checked(_run: run_host.Run, _paths: run_host.RunPaths) -> tuple[Policy, str]:
        return value, "fake-codex"

    def execute(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
        assert kwargs["cwd"] == paths.worktree
        assert kwargs["env"] == value.env
        assert kwargs["timeout"] == 30
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        if mode == "sandbox":
            assert command[:3] == ("fake-codex", "sandbox", "-c")
            assert command[-4:] == ("--", "/bin/sh", "-c", command_text)
            assert "input" not in kwargs
        else:
            assert command[1] == "exec"
            assert "-m" not in command
            assert command[-2:] == ("--json", "-")
            assert (
                kwargs["input"]
                == f"Run this exact shell command once, then finish: {command_text}"
            )
        item = {
            "type": "command_execution" if execution_event else "agent_message",
            "text": "The sandbox failed to initialize",
        }
        return CompletedProcess(
            command,
            exit_code,
            stdout=json.dumps({"type": "item.completed", "item": item}),
            stderr="fixture stderr",
        )

    module = sys.modules[__name__]
    monkeypatch.setattr(run_host, "checked_policy", checked)
    monkeypatch.setattr(module, "checked_live_policy", checked)
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(run=execute))
    if mode == "sandbox":
        result = execute_sandbox_git_probe(run, paths, command_text)
        assert result.returncode == exit_code
        assert result.stderr == "fixture stderr"
    elif exit_code:
        with pytest.raises(AssertionError, match="no successful harness evidence"):
            execute_native_git_probe(run, paths, command_text)
    else:
        assert execute_native_git_probe(run, paths, command_text) is execution_event
