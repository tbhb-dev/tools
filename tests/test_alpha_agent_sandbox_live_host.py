"""Host observations for protected hard links and terminal input injection."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from agent_live_host_probe import (
    assert_unchanged,
    checked_live_policy,
    hardlink_attempt,
    link_control,
    live_policy,
    protected_sources,
    snapshot,
)
from test_alpha_agent_sandbox_live import report, sandbox_command

from tbhbdev.alpha.agent.model import Harness, Invalid, Profile


@pytest.mark.parametrize("operation", ["write", "chmod"])
def test_hardlink_observer_rejects_a_changed_target(
    tmp_path: Path, operation: str
) -> None:
    source = tmp_path / "protected"
    source.write_text("original")
    source.chmod(0o600)
    before = snapshot(source)
    link = tmp_path / "alias"
    os.link(source, link)
    if operation == "write":
        link.write_text("changed")
    else:
        link.chmod(0o700)
    with pytest.raises(AssertionError):
        assert_unchanged(source, before)


def test_hardlink_control_and_probe_use_the_same_filesystem(tmp_path: Path) -> None:
    source = tmp_path / "protected"
    source.write_text("original")
    source.chmod(0o600)
    writes = tmp_path / "writes"
    writes.mkdir()
    control = tmp_path / "control"
    control.mkdir()
    ln = shutil.which("ln")
    assert ln is not None
    link_control(source, writes, control, ln)
    assert not (writes / source.name).exists()
    assert (control / source.name).stat().st_ino == source.stat().st_ino
    assert hardlink_attempt(source, writes / source.name, ln) == {
        "link": 0,
        "read": 0,
        "write": 0,
        "chmod": 0,
    }
    assert source.read_text() == "changed"
    assert source.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="hard links: failed evidence; set TBHBDEV_SANDBOX_LIVE=1",
)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
def test_live_protected_hardlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, profile: Profile
) -> None:
    installed = shutil.which(harness.value)
    if installed is None:
        pytest.skip(f"{harness} {profile} hard links: failed evidence; not installed")
    run, paths = live_policy(tmp_path, monkeypatch, harness, profile)
    sources = protected_sources(run)
    writes = Path(paths.audit_dir)
    writes.mkdir(parents=True)
    controls = tmp_path / "controls"
    controls.mkdir()
    ln = shutil.which("ln")
    assert ln is not None
    for source in sources:
        link_control(source, writes, controls, ln)
    before = [snapshot(source) for source in sources]
    results = writes / "links.json"
    helper = Path(__file__).with_name("agent_live_host_probe.py")
    script = tmp_path / "links.sh"
    script.write_text(
        shlex.join(
            (
                sys.executable,
                str(helper),
                "hardlinks",
                str(results),
                str(writes),
                ln,
                *(str(p) for p in sources),
            )
        )
        + "\n"
    )
    run.files.write(
        "prompt",
        "Execute this exact command with Bash, then finish: "
        f"/bin/sh {shlex.quote(str(script))}",
    )
    try:
        value, executable = checked_live_policy(run, paths)
    except Invalid:
        pytest.skip(f"{harness} {profile} hard links: failed evidence; policy refused")
    command = sandbox_command(run, paths, value, executable, script)
    result = subprocess.run(
        command,
        input=run.files.path("prompt").read_text(),
        env=value.env,
        cwd=paths.worktree,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    for source, expected in zip(sources, before, strict=True):
        assert_unchanged(source, expected)
    if not results.is_file():
        pytest.skip(
            f"{harness} {profile} hard links: failed evidence; "
            f"probe not executed (exit {result.returncode})"
        )
    observed = json.loads(results.read_text())
    assert set(observed) == {p.name for p in sources}
    assert all(
        set(attempts) == {"link", "read", "write", "chmod"}
        for attempts in observed.values()
    )
    assert observed[".gitconfig"]["read"] == 0, "stand-in was not readable"
    report(
        f"{harness} {profile} hard links: blocked; equal st_dev; ln control succeeded"
    )
