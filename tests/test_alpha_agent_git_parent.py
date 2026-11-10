"""Real copying clones and git plants exercise the harness parent's environment."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import agent_git_plants
import pytest
from agent_git_plants import PLANTS, plant
from agent_host_example import git, repository
from agent_live_evidence import prepared_codex_home
from test_alpha_agent_run_host import make_run

from tbhbdev.alpha.agent import credentials, host_tools, run_host, worktree
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import Harness, Os


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

    def origin_url(_repo: str) -> str:
        return str(origin)

    monkeypatch.setattr(worktree.repo, "ssh_url", origin_url)
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
        workspace = worktree.create(clone, run.files.name, run.request)
    return run, dataclasses.replace(
        run_host.planned_paths(run), worktree=str(workspace)
    )


@pytest.mark.parametrize("name", PLANTS)
def test_git_parent_overrides_disable_real_plants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, paths = real_clone(tmp_path, monkeypatch, Harness.CODEX)
    marker, target = plant(Path(paths.worktree), tmp_path, name)
    git(target, "status", "--porcelain")
    assert marker.read_text() == "invoked"
    marker.rename(tmp_path / "positive")
    environment = policy.environment(
        run.settings, run.host, Harness.CODEX, paths, "agent"
    )
    subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=target,
        env=environment,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert not marker.exists()


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


@pytest.mark.parametrize("existing", [False, True])
def test_account_hook_probe_restores_hooks_and_quarantines_tool_writes(
    tmp_path: Path, existing: bool
) -> None:
    home = tmp_path / "account"
    config = home / ".codex"
    config.mkdir(parents=True)
    source = config / "hooks.json"
    if existing:
        source.write_text("original fixture hooks")
    codex_home = tmp_path / "prepared"
    codex_home.mkdir()

    def fail_probe() -> None:
        with agent_git_plants.account_hook_probe(home, codex_home, tmp_path) as (
            command,
            _marker,
            written,
        ):
            assert source.exists()
            subprocess.run(["/bin/sh", "-c", command], check=True, timeout=5)
            assert written.exists()
            message = "probe failed"
            raise ValueError(message)

    with pytest.raises(ValueError, match="probe failed"):
        fail_probe()
    assert not (codex_home / "hooks.json").exists()
    if existing:
        assert source.read_text() == "original fixture hooks"
    else:
        assert not source.exists()


@pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="set TBHBDEV_SANDBOX_LIVE=1 for real harness git probes",
)
@pytest.mark.parametrize("name", PLANTS)
@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_live_harness_parent_ignores_each_git_plant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, harness: Harness
) -> None:
    account_home = Path.home()
    auth_home = prepared_codex_home(account_home) if harness is Harness.CODEX else None
    installed = shutil.which(harness.value)
    assert installed is not None, f"{harness}: harness binary not installed"
    run, paths = real_clone(tmp_path, monkeypatch, harness)
    sandbox = dataclasses.replace(
        run.settings.sandbox,
        host_path=(str(Path(installed).parent),),
        write_roots=(),
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
    marker, target = plant(Path(paths.worktree), tmp_path, name)
    git(target, "status", "--porcelain")
    assert marker.exists(), "plain git status did not fire the positive control"
    marker.rename(tmp_path / "positive")
    control = Path(paths.worktree) / "shell-control"
    command_text = f"printf invoked > {shlex.quote(str(control))}"
    user_marker = tmp_path / "user-hook-marker"
    written = Path(paths.codex_home) / "hooks.json"
    with ExitStack() as stack:
        if auth_home is not None:
            paths = dataclasses.replace(paths, codex_home=auth_home)
            attempt, user_marker, written = stack.enter_context(
                agent_git_plants.account_hook_probe(
                    account_home, Path(auth_home), tmp_path
                )
            )
            command_text = attempt + " || :; " + command_text
        execute_native_git_probe(run, paths, command_text)
        assert not marker.exists(), f"{harness} executed the {name} plant"
        assert control.is_file(), "harness did not execute the shell tool control"
        assert not user_marker.exists(), "Codex loaded the account's user hook"
        assert not written.exists(), "the shell tool wrote hooks into CODEX_HOME"


def execute_native_git_probe(
    run: run_host.Run, paths: run_host.RunPaths, command_text: str
) -> None:
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
        assert any(
            event.get("item", {}).get("type") == "command_execution" for event in events
        )
