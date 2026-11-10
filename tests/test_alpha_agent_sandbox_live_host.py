"""Host observations for protected hard links and terminal input injection."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
from agent_live_host_probe import (
    INPUT_MARKER,
    TerminalObservation,
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

from tbhbdev.alpha.agent import host_tools, run_host
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


@pytest.fixture
def fake_host_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()

    def fake_home() -> Path:
        return home

    monkeypatch.setattr(Path, "home", fake_home)
    executable = tmp_path / "bin" / "harness"
    executable.parent.mkdir()
    executable.write_bytes(b"\x7fELF")
    executable.chmod(0o700)
    which = shutil.which

    def installed(name: str) -> str | None:
        return str(executable) if name in {h.value for h in Harness} else which(name)

    def resolved(_name: str, _path: str | None = None) -> str:
        return str(executable)

    monkeypatch.setattr(shutil, "which", installed)
    monkeypatch.setattr(host_tools, "resolve", resolved)


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("profile", Profile)
def test_hard_link_evidence_uses_profile_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: Profile
) -> None:
    def command(
        _run: object, _paths: object, _value: object, _executable: str, script: Path
    ) -> tuple[str, ...]:
        return "fake-sandbox", str(script)

    def execute(argv: tuple[str, ...], **_kwargs: object) -> CompletedProcess[str]:
        assert argv[0] == "fake-sandbox"
        _, _, _, results, writes, _, *sources = shlex.split(Path(argv[1]).read_text())
        expected = tmp_path / ("temp" if profile is Profile.BUILD else "audit")
        assert Path(writes) == expected
        assert Path(results).parent == expected
        assert len(sources) == 4
        assert Path(sources[-1]) == tmp_path / "unlisted"
        Path(results).write_text(
            json.dumps(
                {
                    Path(p).name: {"read": 0, "link": 1, "write": 1, "chmod": 1}
                    for p in sources
                }
            )
        )
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(sys.modules[__name__], "probe_command", command)
    monkeypatch.setattr(
        sys.modules[__name__], "subprocess", SimpleNamespace(run=execute)
    )
    test_live_protected_hard_links(tmp_path, monkeypatch, Harness.CODEX, profile)


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
    writes = Path(paths.tmpdir if profile is Profile.BUILD else paths.audit_dir)
    writes.mkdir(parents=True, exist_ok=True)
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
    assert observed[sources[0].name]["read"] == 0, "stand-in was not readable"
    report(
        f"{harness} {profile} hard links: blocked; equal st_dev; ln control succeeded"
    )


@pytest.mark.parametrize(
    ("control", "probe", "error", "expected"),
    [
        (b"", b"", 1, "unproven"),
        (b"", INPUT_MARKER, 1, "injected"),
        (INPUT_MARKER, b"", 1, "blocked"),
        (INPUT_MARKER, INPUT_MARKER, 1, "injected"),
        (INPUT_MARKER, b"", 0, "injected"),
        (b"", b"", 0, "injected"),
    ],
)
def test_terminal_verdict_requires_input_control(
    control: bytes, probe: bytes, error: int, expected: str
) -> None:
    assert terminal_status(control, probe, error) == expected


def test_terminal_output_is_not_input(tmp_path: Path) -> None:
    result = observe_terminal(
        (sys.executable, "-c", "print('agent-input-probe')"),
        {"PATH": "/usr/bin:/bin"},
        tmp_path,
    )
    assert result.code == 0
    assert result.inputs == b""


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("error", [0, 1])
@pytest.mark.parametrize("profile", Profile)
def test_terminal_probe_requires_ioctl_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int, profile: Profile
) -> None:
    def observe(
        command: tuple[str, ...], _environment: dict[str, str], _cwd: Path
    ) -> TerminalObservation:
        target = Path(command[-1])
        sandbox = target.suffix == ".sh"
        witness = Path(shlex.split(target.read_text())[-1]) if sandbox else target
        assert witness.parent == tmp_path / (
            "temp" if profile is Profile.BUILD else "audit"
        )
        witness.write_text(
            json.dumps({"attempted": True, "error": error if sandbox else 0})
        )
        return TerminalObservation(b"" if sandbox else INPUT_MARKER, 0)

    monkeypatch.setattr(sys.modules[__name__], "observe_terminal", observe)
    if error == 0:
        with pytest.raises(AssertionError, match="ioctl error=0"):
            test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)
    else:
        test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)


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
    writes = Path(paths.tmpdir if profile is Profile.BUILD else paths.audit_dir)
    writes.mkdir(parents=True, exist_ok=True)
    helper = Path(__file__).with_name("agent_live_host_probe.py")
    control_witness = writes / "control-terminal.json"
    control = observe_terminal(
        (sys.executable, str(helper), "inject", str(control_witness)),
        {"HOME": paths.home, "PATH": "/usr/bin:/bin"},
        tmp_path,
    )
    control_state = "injected" if INPUT_MARKER in control.inputs else "failed"
    report(f"{label} control: {control_state}; legacy_tiocsti={setting}")
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
    observed = json.loads(witness.read_text())
    assert observed["attempted"] is True
    error = observed["error"]
    assert isinstance(error, int)
    status = terminal_status(control.inputs, probe.inputs, error)
    report(f"{label}: {status}; ioctl error={error}; legacy_tiocsti={setting}")
    if status == "unproven":
        pytest.skip(f"{label}: unproven; control failed; legacy_tiocsti={setting}")
    assert status == "blocked", f"{label}: injected; ioctl error={error}"


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


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
@pytest.mark.parametrize("profile", Profile)
def test_host_probes_use_readable_protection_and_selected_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, profile: Profile
) -> None:
    executable = tmp_path / "bin" / harness.value
    executable.parent.mkdir()
    executable.write_bytes(b"\x7fELF")
    executable.chmod(0o700)

    def installed(_name: str) -> str:
        return str(executable)

    def resolved(_name: str, _path: str | None = None) -> str:
        return str(executable)

    monkeypatch.setattr(shutil, "which", installed)
    monkeypatch.setattr(host_tools, "resolve", resolved)
    run, paths = live_policy(tmp_path, monkeypatch, harness, profile)
    readable, _, _, unlisted = protected_sources(run)
    value, selected = checked_live_policy(run, paths)
    assert selected == str(executable)
    assert str(readable) in run_host.protected_home(run)
    assert all(not readable.is_relative_to(deny) for deny in value.deny)
    entries = (*run_host.protected_home(run), *value.deny)
    assert all(not unlisted.is_relative_to(entry) for entry in entries)
    run.files.write("prompt", "probe")
    command = probe_command(run, paths, value, selected, tmp_path / "probe.sh")
    if harness is Harness.CODEX:
        assert command[command.index("-P") + 1] == profile.value
    else:
        assert command[-1] == "probe"
        settings = json.loads(command[command.index("--settings") + 1])
        assert str(readable) in settings["sandbox"]["filesystem"]["denyWrite"]
