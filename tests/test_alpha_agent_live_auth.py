"""Live sign-in uses production copying, refresh write-back and removal."""

import dataclasses
import json
import stat
import sys
from pathlib import Path
lazy from collections.abc import Callable

import agent_live_evidence
import pytest
import test_alpha_agent_git_parent as parent
from test_alpha_agent_run_host import make_run

from tbhbdev.alpha.agent import run_host
from tbhbdev.alpha.agent.model import Harness


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("account_id", [None, "fixture-account"])
def test_live_codex_auth_refresh_is_written_back_and_removed(
    tmp_path: Path, failure: bool, account_id: str | None
) -> None:
    root = tmp_path / "probe"
    run = make_run(root)
    account = tmp_path / "account"
    source = account / ".codex/auth.json"
    source.parent.mkdir(parents=True)
    # Codex 0.164.0 TokenData serializes OAuth provenance as a nested object:
    # https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/login/src/token_data.rs
    tokens = {
        "id_token": "fixture-id",
        "access_token": "fixture-access",
        "refresh_token": "fixture-before",
        "oauth_provenance": {
            "issuer": "https://auth.openai.com",
            "client_id": "fixture-client",
        },
        "login_owner_id": "fixture-owner",
        "account_id": account_id,
    }
    before = json.dumps({"tokens": tokens})
    after = json.dumps({"tokens": tokens | {"refresh_token": "fixture-after"}})
    source.write_text(before)
    paths = dataclasses.replace(
        run_host.planned_paths(run), codex_home=str(root / "codex")
    )

    def probe() -> None:
        with agent_live_evidence.codex_auth(run, paths, account, root):
            directory = Path(paths.codex_home)
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert (directory / "auth.json").read_text() == before
            (directory / "auth.json").write_text(after)
            if failure:
                message = "fixture probe failed"
                raise RuntimeError(message)

    if failure:
        with pytest.raises(RuntimeError, match="fixture probe failed"):
            probe()
    else:
        probe()
    assert source.read_text() == after
    assert not list(root.rglob("auth.json"))
    assert not Path(paths.codex_home).exists()


@pytest.mark.parametrize("temp_write", [False, True])
def test_live_git_probe_plants_hooks_only_in_the_scratch_run_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_account_codex_writes: Callable[[Path], None],
    temp_write: bool,
) -> None:
    account = tmp_path / "account"
    source = account / ".codex/auth.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": "fixture-id",
                    "access_token": "fixture-access",
                    "refresh_token": "fixture",
                }
            }
        )
    )
    account_hooks = source.parent / "hooks.json"
    account_hooks.write_text("original fixture hooks")
    root = tmp_path / "probe"
    run = make_run(root)
    paths = dataclasses.replace(
        run_host.planned_paths(run), codex_home=str(root / "codex")
    )
    workspace = Path(paths.worktree)
    workspace.mkdir(parents=True)
    marker = root / "git-marker"
    owner = sys.modules[guard_account_codex_writes.__module__]
    monkeypatch.setattr(owner, "ACCOUNT_CODEX_HOME", source.parent)
    monkeypatch.setenv("HOME", str(account))

    def clone(
        _root: Path, patch: pytest.MonkeyPatch, _harness: Harness
    ) -> tuple[run_host.Run, run_host.RunPaths]:
        patch.setenv("HOME", str(run.home))
        return run, paths

    def installed(_name: str) -> str:
        return "/example/codex"

    def plant(_workspace: Path, _root: Path, _name: str) -> tuple[Path, Path]:
        return marker, workspace

    def positive(_target: Path, *_args: str) -> str:
        marker.write_text("invoked")
        return ""

    def native(
        actual: run_host.Run, actual_paths: run_host.RunPaths, _command: str
    ) -> None:
        assert actual.home == run.home
        assert actual.settings.sandbox.write_roots == (
            (str(root / "harness-write"),) if temp_write else ()
        )
        if temp_write:
            assert (root / "harness-write").is_dir()
        assert (run.home / ".codex/hooks.json").is_file()
        assert account_hooks.read_text() == "original fixture hooks"
        assert [p.name for p in Path(actual_paths.codex_home).iterdir()] == [
            "auth.json"
        ]
        (workspace / "shell-control").write_text("invoked")

    monkeypatch.setattr(parent, "real_clone", clone)
    monkeypatch.setattr(parent.shutil, "which", installed)
    monkeypatch.setattr(parent, "plant", plant)
    monkeypatch.setattr(parent, "git", positive)
    monkeypatch.setattr(parent, "execute_native_git_probe", native)
    parent.test_live_harness_parent_ignores_each_git_plant(
        root, monkeypatch, "config", Harness.CODEX, temp_write=temp_write
    )
    assert account_hooks.read_text() == "original fixture hooks"
    assert not list(root.rglob("auth.json"))
