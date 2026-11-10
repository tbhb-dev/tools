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
    hard_link_attempt,
    legacy_input_setting,
    link_control,
    live_policy,
    main,
    observe_terminal,
    probe_command,
    protected_sources,
    snapshot,
    terminal_status,
)
from test_alpha_agent_sandbox_live import report

from tbhbdev.alpha.agent.model import Harness, Invalid, Profile


@pytest.mark.parametrize("operation", ["write", "chmod"])
def test_hard_link_observer_rejects_a_changed_target(
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


def test_hard_link_control_and_probe_use_the_same_filesystem(tmp_path: Path) -> None:
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
    assert hard_link_attempt(source, writes / source.name, ln) == {
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
def test_live_protected_hard_links(
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
                "hard_links",
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
    command = probe_command(run, paths, value, executable, script)
    result = subprocess.run(
        command,
        env=value.env,
        cwd=paths.worktree,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    unchanged = [
        snapshot(source) == expected
        for source, expected in zip(sources, before, strict=True)
    ]
    for source, safe in zip(sources, unchanged, strict=True):
        report(
            f"{harness} {profile} {source.name}: {'unchanged' if safe else 'changed'}"
        )
    assert all(unchanged), "protected hard-link targets changed"
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


@pytest.mark.parametrize(
    ("control", "probe", "expected"),
    [
        (b"", b"", "unproven"),
        (b"", b"agent-input-probe", "unproven"),
        (b"agent-input-probe", b"", "blocked"),
        (b"agent-input-probe", b"agent-input-probe", "injected"),
    ],
)
def test_terminal_verdict_requires_input_control(
    control: bytes, probe: bytes, expected: str
) -> None:
    assert terminal_status(control, probe) == expected


def test_terminal_output_is_not_input(tmp_path: Path) -> None:
    result = observe_terminal(
        (sys.executable, "-c", "print('agent-input-probe')"),
        {"PATH": "/usr/bin:/bin"},
        tmp_path,
    )
    assert result.code == 0
    assert result.inputs == b""


@pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="terminal: unproven; set TBHBDEV_SANDBOX_LIVE=1",
)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
def test_live_terminal_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, profile: Profile
) -> None:
    setting = legacy_input_setting()
    label = f"{harness} {profile} terminal"
    installed = shutil.which(harness.value)
    if installed is None:
        pytest.skip(f"{label}: unproven; not installed; legacy_tiocsti={setting}")
    run, paths = live_policy(tmp_path, monkeypatch, harness, profile)
    writes = Path(paths.audit_dir)
    writes.mkdir(parents=True)
    helper = Path(__file__).with_name("agent_live_host_probe.py")
    control_witness = writes / "control-terminal.json"
    control = observe_terminal(
        (sys.executable, str(helper), "inject", str(control_witness)),
        {"HOME": paths.home, "PATH": "/usr/bin:/bin"},
        tmp_path,
    )
    witness = writes / "sandbox-terminal.json"
    script = tmp_path / "terminal.sh"
    script.write_text(
        shlex.join((sys.executable, str(helper), "inject", str(witness))) + "\n"
    )
    run.files.write(
        "prompt",
        "Execute this exact command with Bash, then finish: "
        + shlex.join(("/bin/sh", str(script))),
    )
    try:
        value, executable = checked_live_policy(run, paths)
    except Invalid:
        pytest.skip(f"{label}: unproven; policy refused; legacy_tiocsti={setting}")
    probe = observe_terminal(
        probe_command(run, paths, value, executable, script),
        value.env,
        Path(paths.worktree),
    )
    if not witness.is_file():
        pytest.skip(f"{label}: unproven; probe not executed; legacy_tiocsti={setting}")
    assert json.loads(witness.read_text())["attempted"] is True
    status = terminal_status(control.inputs, probe.inputs)
    report(f"{label}: {status}; legacy_tiocsti={setting}")
    if status == "unproven":
        pytest.skip(f"{label}: unproven; control failed; legacy_tiocsti={setting}")
    assert status == "blocked", f"{label}: injected bytes reached terminal input"


def test_link_probe_records_every_label_after_a_denial(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("original")
    writes = tmp_path / "writes"
    writes.mkdir()
    results = writes / "results.json"
    ln = shutil.which("ln")
    assert ln is not None
    main(
        "hardlinks",
        [str(results), str(writes), ln, str(tmp_path / "absent"), str(source)],
    )
    observed = json.loads(results.read_text())
    assert observed["absent"]["link"] != 0
    assert observed["absent"]["write"] != 0
    assert observed["absent"]["chmod"] != 0
    assert observed["source"] == {"read": 0, "link": 0, "write": 0, "chmod": 0}
