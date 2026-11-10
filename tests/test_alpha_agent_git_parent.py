"""Real copying clones and git plants exercise the harness parent's environment."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import agent_git_plants
import pytest
from agent_git_plants import PLANTS, plant
from agent_host_example import git, repository
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
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox, host_path=(str(directory),)
            ),
        ),
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


@pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="set TBHBDEV_SANDBOX_LIVE=1 for real harness git probes",
)
@pytest.mark.parametrize("name", PLANTS)
@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_live_harness_parent_ignores_each_git_plant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, harness: Harness
) -> None:
    installed = shutil.which(harness.value)
    assert installed is not None, f"{harness}: harness binary not installed"
    run, paths = real_clone(tmp_path, monkeypatch, harness)
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox,
                host_path=(str(Path(installed).parent),),
                write_roots=(),
            ),
        ),
    )
    marker, target = plant(Path(paths.worktree), tmp_path, name)
    git(target, "status", "--porcelain")
    assert marker.exists(), "plain git status did not fire the positive control"
    marker.rename(tmp_path / "positive")
    control = Path(paths.worktree) / "shell-control"
    command_text = f"printf invoked > {shlex.quote(str(control))}"
    user_marker = tmp_path / "user-hook-marker"
    written = Path(paths.codex_home) / "hooks.json"
    if harness is Harness.CODEX:
        Path(paths.codex_home).mkdir()
        attempt, user_marker, written = agent_git_plants.hook_write_probe(
            run.home, Path(paths.codex_home), tmp_path
        )
        command_text = attempt + " || :; " + command_text
    run.files.write(
        "prompt", f"Run this exact shell command once, then finish: {command_text}"
    )
    value, executable = run_host.checked_policy(run, paths)
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    command = run_host.harness_command(run, prepared, value, executable)
    if harness is Harness.CODEX:
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
    assert not marker.exists(), f"{harness} executed the {name} plant"
    assert result.returncode == 0, f"{harness}: no successful harness evidence"
    assert control.is_file(), "harness did not execute the shell tool control"
    assert not user_marker.exists(), "Codex loaded the account's user hook"
    assert not written.exists(), "the shell tool wrote hooks into CODEX_HOME"
    if harness is Harness.CODEX:
        events = [json.loads(line) for line in result.stdout.splitlines()]
        assert any(
            event.get("item", {}).get("type") == "command_execution" for event in events
        )
