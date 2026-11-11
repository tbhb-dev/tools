"""Every run-host state directory follows the absolute XDG state contract."""

from pathlib import Path

import pytest
from agent_host_example import constant
from test_alpha_agent_run_host import make_run

from tbhbdev.alpha.agent import run_host, worktree


@pytest.mark.parametrize("value", [None, "", "relative/state", "absolute"])
def test_run_host_state_requires_an_absolute_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    run = make_run(tmp_path)
    monkeypatch.setenv("HOME", str(run.home))
    if value == "absolute":
        value = str(tmp_path / "state")
    if value is None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_STATE_HOME", value)
    state = (
        Path(value) if value and value.startswith("/") else run.home / ".local/state"
    )
    assert worktree.mirror_path(run.home, run.request.repo) == (
        state / "tbhbdev/agent-mirrors/github.com/example-org/example-repo.git"
    )
    created: list[str] = []
    private_dir = run_host.runfiles.private_dir

    def record(path: Path) -> Path:
        created.append(str(path))
        return private_dir(path)

    monkeypatch.setattr(run_host.runfiles, "private_dir", record)
    monkeypatch.setattr(run_host.settings, "load", constant(run.settings))
    assert (
        run_host.main(
            ["example", str(tmp_path / "missing-request"), "--host", run.host.name],
            "agent",
        )
        == 1
    )
    assert (state / "tbhbdev/agent-runs/example.rc").read_text() == "1\n"
    assert str(state / "tbhbdev/agent-runs") in created
    assert run_host.planned_paths(run).codex_home == str(
        state / "tbhbdev/agent-codex/example"
    )
