"""Host observations for protected hard links and terminal input injection."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import shlex
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import Never

import agent_live_host_probe as host_probe
import pytest
from agent_host_example import trusted_host_stat
from agent_live_evidence import codex_auth, run_default_pytest
from agent_live_host_probe import (
    INPUT_MARKER,
    TerminalAttempt,
    TerminalObservation,
    assert_unchanged,
    checked_live_policy,
    codex_sandbox_command,
    hard_link_attempt,
    legacy_input_setting,
    link_control,
    live_policy,
    observe_terminal,
    probe_command,
    protected_sources,
    snapshot,
    terminal_status,
)
from test_alpha_agent_codex_install import release
from test_alpha_agent_sandbox_live import live_only, probe_run, report

from tbhbdev.alpha.agent import credentials, host_tools, run_host
from tbhbdev.alpha.agent.model import Harness, Invalid, Os, Profile


def test_host_live_probes_skip_without_opt_in(tmp_path: Path) -> None:
    result = run_default_pytest(tmp_path, "-q", __file__, "-k", "test_live_")
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "skipped" in result.stdout
    assert "passed" not in result.stdout


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
    executable = release(tmp_path / "install") / "bin/codex"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    executable.write_bytes(b"\x7fELF")
    executable.chmod(0o700)
    which = shutil.which

    def installed(name: str) -> str | None:
        return str(executable) if name in {h.value for h in Harness} else which(name)

    def resolved(
        _name: str, _path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        assert follow_symlinks is False
        return str(executable)

    monkeypatch.setattr(shutil, "which", installed)
    monkeypatch.setattr(host_tools, "resolve", resolved)
    trusted_host_stat(monkeypatch)

    def no_auth(_source: Path, _destination: Path) -> None:
        return None

    monkeypatch.setattr(credentials, "auth_copy", no_auth)

    def command(
        _run: object, _paths: object, _value: object, _executable: str, script: Path
    ) -> tuple[str, ...]:
        return "fake-sandbox", str(script)

    monkeypatch.setattr(sys.modules[__name__], "probe_command", command)


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
        assert _kwargs.get("input") == (tmp_path / "runs/probe.prompt").read_text()
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


@live_only
@pytest.mark.parametrize("harness", [Harness.CODEX])
@pytest.mark.parametrize("profile", Profile)
def test_live_protected_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, profile: Profile
) -> None:
    installed = shutil.which(harness.value)
    if installed is None:
        pytest.fail(f"{harness} {profile} hard links: failed evidence; not installed")
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
        pytest.fail(f"{harness} {profile} hard links: failed evidence; policy refused")
    command = probe_command(run, paths, value, executable, script)
    with codex_auth(run, paths, run.home, tmp_path):
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
        pytest.fail(
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
    assert observed[sources[-1].name]["read"] == 0, "unlisted source was not readable"
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
    assert (
        terminal_status(control, probe, TerminalAttempt(attempted=True, error=error))
        == expected
    )


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
        command: tuple[str, ...],
        _environment: dict[str, str],
        _cwd: Path,
        *,
        prompt: str | None = None,
        tty_file: Path | None = None,
    ) -> TerminalObservation:
        assert isinstance(tty_file, Path)
        assert prompt is None or isinstance(prompt, str)
        target = Path(command[-1])
        sandbox = target.suffix == ".sh"
        if sandbox:
            assert prompt == (tmp_path / "runs/probe.prompt").read_text()
        witness = Path(shlex.split(target.read_text())[-1]) if sandbox else target
        assert witness.parent == tmp_path / (
            "temp" if profile is Profile.BUILD else "audit"
        )
        witness.write_text(
            json.dumps(terminal_record(attempted=True, error=error if sandbox else 0))
        )
        return TerminalObservation(b"" if sandbox else INPUT_MARKER, 0)

    monkeypatch.setattr(sys.modules[__name__], "observe_terminal", observe)
    if error == 0:
        with pytest.raises(AssertionError, match="ioctl error=0"):
            test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)
    else:
        test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)


def fake_terminal_observation(
    failure: str,
    exit_code: int,
    command: tuple[str, ...],
    _environment: dict[str, str],
    _cwd: Path,
    *,
    prompt: str | None = None,
    tty_file: Path | None = None,
) -> TerminalObservation:
    assert isinstance(tty_file, Path)
    assert prompt is None or isinstance(prompt, str)
    target = Path(command[-1])
    sandbox = target.suffix == ".sh"
    witness = Path(shlex.split(target.read_text())[-1]) if sandbox else target
    if not sandbox or failure != "not-executed":
        witness.write_text(
            json.dumps(terminal_record(attempted=True, error=1 if sandbox else 0))
        )
    inputs = INPUT_MARKER if not sandbox and failure != "control-failed" else b""
    return TerminalObservation(inputs, exit_code if sandbox else 0)


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize(
    "case",
    [
        (kind, failure, code)
        for kind in ("hard_links", "terminal")
        for failure in (
            "missing-harness",
            "policy-refused",
            "not-executed",
            "control-failed",
        )
        for code in (0, 7)
    ],
    ids=str,
)
def test_host_probe_no_evidence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    case: tuple[str, str, int],
) -> None:
    kind, failure, exit_code = case

    def missing(_name: str) -> None:
        return None

    def refused(*_args: object) -> Never:
        message = "synthetic policy refusal"
        raise Invalid(message)

    def failed_control(*_args: object) -> Never:
        message = "synthetic link control failure"
        raise OSError(message)

    def execute(command: tuple[str, ...], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, exit_code)

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(run=execute))
    monkeypatch.setattr(
        module,
        "observe_terminal",
        partial(fake_terminal_observation, failure, exit_code),
    )
    if failure == "missing-harness":
        monkeypatch.setattr(shutil, "which", missing)
    elif failure == "policy-refused":
        monkeypatch.setattr(module, "checked_live_policy", refused)
    elif failure == "control-failed" and kind == "hard_links":
        monkeypatch.setattr(module, "link_control", failed_control)
    probe = (
        test_live_protected_hard_links
        if kind == "hard_links"
        else test_live_terminal_injection
    )
    with pytest.raises(
        (pytest.fail.Exception, pytest.skip.Exception, OSError)
    ) as caught:
        probe(tmp_path, monkeypatch, Harness.CODEX, profile)
    if kind == "hard_links" and failure == "control-failed":
        assert isinstance(caught.value, OSError)
        assert str(caught.value) == "synthetic link control failure"
    else:
        assert isinstance(caught.value, pytest.fail.Exception)


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize(
    "failure", ["no-witness", "ioctl-error", "no-attempt", "exit-error"]
)
def test_terminal_control_requires_recorded_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def observe(
        command: tuple[str, ...],
        _environment: dict[str, str],
        _cwd: Path,
        *,
        prompt: str | None = None,
        tty_file: Path | None = None,
    ) -> TerminalObservation:
        assert isinstance(tty_file, Path)
        assert prompt is None
        if failure != "no-witness":
            Path(command[-1]).write_text(
                json.dumps(
                    {
                        "attempted": failure != "no-attempt",
                        "error": int(failure == "ioctl-error"),
                    }
                )
            )
        return TerminalObservation(INPUT_MARKER, int(failure == "exit-error"))

    monkeypatch.setattr(sys.modules[__name__], "observe_terminal", observe)
    monkeypatch.setattr(sys.modules[__name__], "legacy_input_setting", lambda: "1")
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
        test_live_terminal_injection(
            tmp_path, monkeypatch, Harness.CODEX, Profile.BUILD
        )
    assert isinstance(caught.value, pytest.fail.Exception)
    assert "control failed" in str(caught.value)


def terminal_control_state(*, kernel_blocked: bool, inputs: bytes) -> str:
    if kernel_blocked:
        return "blocked by kernel"
    return "injected" if INPUT_MARKER in inputs else "failed"


def terminal_record(attempted: bool, error: int) -> dict[str, object]:
    return {
        "attempted": attempted,
        "error": error,
        "inherited": [
            {"descriptor": fd, "attempted": True, "error": 1} for fd in range(3)
        ],
    }


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("descriptor", [None, 0, 1, 2])
def test_terminal_legacy_input_enabled_checks_inherited_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor: int | None
) -> None:
    def observe(
        command: tuple[str, ...],
        _environment: dict[str, str],
        _cwd: Path,
        *,
        prompt: str | None = None,
        tty_file: Path | None = None,
    ) -> TerminalObservation:
        assert tty_file is not None
        tty_file.write_text("/dev/pts/123")
        target = Path(command[-1])
        sandbox = target.suffix == ".sh"
        assert (prompt is not None) is sandbox
        witness = Path(shlex.split(target.read_text())[-1]) if sandbox else target
        document = terminal_record(not sandbox, errno.ENOENT if sandbox else 0)
        document["inherited"] = [
            {
                "descriptor": fd,
                "attempted": True,
                "error": 0 if fd == descriptor else errno.EPERM,
            }
            for fd in range(3)
        ]
        witness.write_text(json.dumps(document))
        return TerminalObservation(b"" if sandbox else INPUT_MARKER, 0)

    module = sys.modules[__name__]
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(module, "observe_terminal", observe)
    monkeypatch.setattr(module, "legacy_input_setting", lambda: "1")
    if descriptor is None:
        test_live_terminal_injection(
            tmp_path, monkeypatch, Harness.CODEX, Profile.BUILD
        )
    else:
        with pytest.raises(AssertionError):
            test_live_terminal_injection(
                tmp_path, monkeypatch, Harness.CODEX, Profile.BUILD
            )


@live_only
@pytest.mark.parametrize("harness", [Harness.CODEX])
@pytest.mark.parametrize("profile", Profile)
def test_live_terminal_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, profile: Profile
) -> None:
    setting = legacy_input_setting()
    label = f"{harness} {profile} terminal"
    installed = shutil.which(harness.value)
    if installed is None:
        pytest.fail(f"{label}: unproven; not installed; legacy_tiocsti={setting}")
    run, paths = live_policy(tmp_path, monkeypatch, harness, profile)
    writes = Path(paths.tmpdir if profile is Profile.BUILD else paths.audit_dir)
    writes.mkdir(parents=True, exist_ok=True)
    helper = Path(__file__).with_name("agent_live_host_probe.py")
    tty_file = tmp_path / "terminal-path"
    control_witness = writes / "control-terminal.json"
    control = observe_terminal(
        (sys.executable, str(helper), "inject", str(tty_file), str(control_witness)),
        {"HOME": paths.home, "PATH": "/usr/bin:/bin"},
        tmp_path,
        tty_file=tty_file,
    )
    if control.code != 0 or not control_witness.is_file():
        pytest.fail(f"{label}: unproven; control failed; legacy_tiocsti={setting}")
    control_result = json.loads(control_witness.read_text())
    control_error = control_result.get("error")
    kernel_blocked = (
        setting in {"0", "unavailable"}
        and control_result.get("attempted") is True
        and isinstance(control_error, int)
        and control_error != 0
        and INPUT_MARKER not in control.inputs
    )
    if control_result.get("attempted") is not True or (
        control_error != 0 and not kernel_blocked
    ):
        pytest.fail(f"{label}: unproven; control failed; legacy_tiocsti={setting}")
    state = terminal_control_state(kernel_blocked=kernel_blocked, inputs=control.inputs)
    report(f"{label} control: {state}; legacy_tiocsti={setting}")
    witness = writes / "sandbox-terminal.json"
    script = tmp_path / "terminal.sh"
    script.write_text(
        shlex.join((sys.executable, str(helper), "inject", str(tty_file), str(witness)))
        + "\n"
    )
    run.files.write(
        "prompt",
        "Execute this exact command with Bash, then finish: "
        + shlex.join(("/bin/sh", str(script))),
    )
    try:
        value, executable = checked_live_policy(run, paths)
    except Invalid:
        pytest.fail(f"{label}: unproven; policy refused; legacy_tiocsti={setting}")
    with codex_auth(run, paths, run.home, tmp_path):
        probe = observe_terminal(
            probe_command(run, paths, value, executable, script),
            value.env,
            Path(paths.worktree),
            prompt=run.files.path("prompt").read_text(),
            tty_file=tty_file,
        )
    if not witness.is_file():
        pytest.fail(f"{label}: unproven; probe not executed; legacy_tiocsti={setting}")
    observed = json.loads(witness.read_text())
    attempted = observed["attempted"]
    assert isinstance(attempted, bool)
    error = observed["error"]
    assert isinstance(error, int)
    status = terminal_status(
        control.inputs,
        probe.inputs,
        TerminalAttempt(
            attempted=attempted,
            error=error,
            missing_linux_tty=(
                not attempted
                and run.host.os is Os.LINUX
                and Path(tty_file.read_text()).parent == Path("/dev/pts")
            ),
        ),
        kernel_blocked=kernel_blocked,
    )
    operation = "ioctl" if attempted else "open"
    report(f"{label}: {status}; {operation} error={error}; legacy_tiocsti={setting}")
    inherited = host_probe.inherited_terminal_statuses(
        control.inputs,
        probe.inputs,
        observed["inherited"],
        kernel_blocked=kernel_blocked,
    )
    for descriptor, result in inherited.items():
        report(f"{label} fd={descriptor}: {result}; legacy_tiocsti={setting}")
    if status == "unproven":
        pytest.fail(f"{label}: unproven; control failed; legacy_tiocsti={setting}")
    assert all(
        result in {"blocked", "blocked by kernel"} for result in inherited.values()
    )
    assert status in {"blocked", "blocked by kernel", "blocked by device namespace"}, (
        f"{label}: injected; {operation} error={error}"
    )


def test_link_probe_records_every_label_after_a_denial(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("original")
    writes = tmp_path / "writes"
    writes.mkdir()
    results = writes / "results.json"
    ln = shutil.which("ln")
    assert ln is not None
    subprocess.run(
        (
            sys.executable,
            "-S",
            str(Path(__file__).with_name("agent_live_host_probe.py")),
            "hardlinks",
            str(results),
            str(writes),
            ln,
            str(tmp_path / "absent"),
            str(source),
        ),
        env={"PATH": os.defpath},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    executable = release(tmp_path / "install") / "bin/codex"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    executable.write_bytes(b"\x7fELF")
    executable.chmod(0o700)

    def installed(_name: str) -> str:
        return str(executable)

    def resolved(
        _name: str, _path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        assert follow_symlinks is False
        return str(executable)

    monkeypatch.setattr(shutil, "which", installed)
    monkeypatch.setattr(host_tools, "resolve", resolved)
    trusted_host_stat(monkeypatch)
    run, paths = live_policy(tmp_path, monkeypatch, harness, profile)
    readable, _, _, unlisted = protected_sources(run)
    value, selected = checked_live_policy(run, paths)
    assert selected == (
        str(tmp_path / "data/tbhbdev/codex/example-version/bin/codex")
        if harness is Harness.CODEX
        else str(executable)
    )
    assert str(readable) in run_host.protected_home(run)
    assert all(not readable.is_relative_to(deny) for deny in value.deny)
    entries = (*run_host.protected_home(run), *value.deny)
    assert all(not unlisted.is_relative_to(entry) for entry in entries)
    run.files.write("prompt", "probe")
    command = probe_command(run, paths, value, selected, tmp_path / "probe.sh")
    if harness is Harness.CODEX:
        assert f'default_permissions="{profile.value}"' in command
    else:
        assert command[-1] == "probe"
        settings = json.loads(command[command.index("--settings") + 1])
        assert str(readable) in settings["sandbox"]["filesystem"]["denyWrite"]


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize(
    "control",
    [(setting, error) for setting in ("0", "unavailable") for error in (0, 1)],
)
def test_terminal_kernel_refusal_is_a_passing_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: Profile,
    control: tuple[str, int],
) -> None:
    setting, error = control

    def observe(
        command: tuple[str, ...],
        _environment: dict[str, str],
        _cwd: Path,
        *,
        prompt: str | None = None,
        tty_file: Path | None = None,
    ) -> TerminalObservation:
        assert isinstance(tty_file, Path)
        assert prompt is None or isinstance(prompt, str)
        target = Path(command[-1])
        sandbox = target.suffix == ".sh"
        witness = Path(shlex.split(target.read_text())[-1]) if sandbox else target
        witness.write_text(
            json.dumps(terminal_record(attempted=True, error=error if sandbox else 1))
        )
        return TerminalObservation(b"", 0)

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "observe_terminal", observe)
    monkeypatch.setattr(module, "legacy_input_setting", lambda: setting)
    if error == 0:
        with pytest.raises(AssertionError, match="ioctl error=0"):
            test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)
    else:
        test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)
        assert "blocked by kernel" in capsys.readouterr().out


def test_host_live_probes_refuse_agy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run, paths = probe_run(tmp_path, Harness.AGY)
    with pytest.raises(Invalid, match="agy has no OS sandbox"):
        checked_live_policy(run, paths)


def test_terminal_prompt_reaches_the_harness_with_terminal_stdin(
    tmp_path: Path,
) -> None:
    observed = tmp_path / "prompt.json"
    prompt = "Execute the fixture command.\nThen finish."
    script = (
        "import json,sys;from pathlib import Path;"
        f"Path({str(observed)!r}).write_text(json.dumps([sys.stdin.isatty(),sys.argv[-1]]))"
    )
    result = observe_terminal(
        (sys.executable, "-c", script, "-"),
        {"PATH": os.defpath},
        tmp_path,
        prompt=prompt,
        tty_file=tmp_path / "terminal-path",
    )
    assert (tmp_path / "terminal-path").read_text().startswith("/dev/")
    assert result == TerminalObservation(b"", 0)
    assert json.loads(observed.read_text()) == [True, prompt]


@pytest.mark.parametrize("opened", [False, True])
def test_terminal_witness_distinguishes_open_failure_from_ioctl_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: bool
) -> None:
    witness = tmp_path / "terminal.json"
    closed: list[int] = []

    def open_tty(path: str, flags: int) -> int:
        assert path == "/dev/tty"
        assert flags == os.O_RDONLY | os.O_NOCTTY
        if not opened:
            raise OSError(errno.ENXIO, "fixture tty unavailable")
        return 321

    def refused(descriptor: int, operation: int, value: bytes) -> Never:
        assert descriptor in (0, 1, 2, 321)
        assert operation == host_probe.termios.TIOCSTI
        assert value == INPUT_MARKER[:1]
        raise OSError(errno.EPERM, "fixture ioctl refused")

    monkeypatch.setattr(host_probe.os, "open", open_tty)
    monkeypatch.setattr(host_probe.os, "close", closed.append)
    monkeypatch.setattr(host_probe.fcntl, "ioctl", refused)
    host_probe.inject_terminal(witness)
    assert json.loads(witness.read_text()) == terminal_record(
        opened, errno.EPERM if opened else errno.ENXIO
    ) | {
        "descriptor": "/dev/tty",
        "inherited": [
            {"descriptor": fd, "attempted": True, "error": errno.EPERM}
            for fd in range(3)
        ],
    }
    assert closed == ([321] if opened else [])


@live_only
def test_live_codex_copy_denies_account_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "scratch-home"
    fake_auth = home / ".codex/auth.json"
    fake_auth.parent.mkdir(parents=True)
    fake_auth.write_text('"fixture auth only"')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    run, paths = live_policy(tmp_path, monkeypatch, Harness.CODEX, Profile.BUILD)
    value, executable = checked_live_policy(run, paths)
    run.files.write("prompt", "fixture")
    script = tmp_path / "copy-control.sh"
    script.write_text(
        'if (cd "$HOME" && cat .codex/auth.json) >/dev/null 2>&1; then exit 91; fi\n'
        "printf invoked > write-control\n"
    )
    result = subprocess.run(
        codex_sandbox_command(run, paths, value, executable, "/bin/sh", str(script)),
        env=value.env,
        cwd=paths.worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert (Path(paths.worktree) / "write-control").read_text() == "invoked"
    assert fake_auth.read_text() == '"fixture auth only"'
    report("codex standalone-copy-auth: blocked; worktree write: allowed")


@pytest.mark.usefixtures("fake_host_tools")
def test_codex_copy_live_proof_uses_run_host_policy_and_fake_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def execute(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
        # HostSandboxArgs selects the OS at compile time (Codex rust-v0.164.0,
        # codex-rs/cli/src/main.rs:483-486). Words after options are the command.
        assert command[1:3] == ("sandbox", "-c")
        assert command[-3:] == ("--", "/bin/sh", str(tmp_path / "copy-control.sh"))
        assert "macos" not in command
        assert "linux" not in command
        assert 'default_permissions="build"' in command
        table = next(word for word in command if word.startswith("permissions.build="))
        home = tmp_path / "scratch-home"
        assert f'"{home / ".codex"}" = "none"' in table
        assert (home / ".codex/auth.json").read_text() == '"fixture auth only"'
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["HOME"] == str(home)
        (Path(str(kwargs["cwd"])) / "write-control").write_text("invoked")
        return CompletedProcess(command, 0)

    monkeypatch.setattr(
        sys.modules[__name__], "subprocess", SimpleNamespace(run=execute)
    )
    test_live_codex_copy_denies_account_auth(tmp_path, monkeypatch)
    assert (
        "codex standalone-copy-auth: blocked; worktree write: allowed"
        in capsys.readouterr().out
    )


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("failure", ["readable-auth", "missing-write", "changed-auth"])
def test_codex_copy_control_requires_denial_and_worktree_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    shell_run = subprocess.run

    def execute(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
        workspace = Path(str(kwargs["cwd"]))
        if failure == "readable-auth":
            # Outside a sandbox the planted auth is readable: the exact shell
            # probe must fail before it can write its positive control.
            result = shell_run(
                command[-2:],
                cwd=workspace,
                env={"HOME": str(tmp_path / "scratch-home"), "PATH": os.defpath},
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            assert result.returncode == 91
            assert not (workspace / "write-control").exists()
            return result
        if failure == "changed-auth":
            (workspace / "write-control").write_text("invoked")
            (tmp_path / "scratch-home" / ".codex" / "auth.json").write_text("changed")
        return CompletedProcess(command, 0, stderr="fixture sandbox failure")

    monkeypatch.setattr(
        sys.modules[__name__], "subprocess", SimpleNamespace(run=execute)
    )
    with pytest.raises((AssertionError, FileNotFoundError)):
        test_live_codex_copy_denies_account_auth(tmp_path, monkeypatch)


def test_terminal_helper_opens_the_named_tty_without_a_controlling_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tty_file = tmp_path / "tty-path"
    tty_file.write_text("/dev/fixture-tty")
    witness = tmp_path / "witness.json"
    injected: list[bytes] = []
    closed: list[int] = []

    def opened(path: str, flags: int) -> int:
        assert path == "/dev/fixture-tty"
        assert flags == os.O_RDONLY | os.O_NOCTTY
        return 123

    def inject(descriptor: int, operation: int, value: bytes) -> None:
        assert descriptor in (0, 1, 2, 123)
        assert operation == host_probe.termios.TIOCSTI
        injected.append(value)

    monkeypatch.setattr(host_probe.os, "open", opened)
    monkeypatch.setattr(host_probe.os, "close", closed.append)
    monkeypatch.setattr(host_probe.fcntl, "ioctl", inject)
    host_probe.main("inject", [str(tty_file), str(witness)])
    assert b"".join(injected) == INPUT_MARKER * 4
    assert closed == [123]
    assert json.loads(witness.read_text()) == {
        "descriptor": "/dev/fixture-tty",
        "attempted": True,
        "error": 0,
        "inherited": [
            {"descriptor": fd, "attempted": True, "error": 0} for fd in range(3)
        ],
    }


def test_terminal_wrapper_reads_input_before_the_session_leader_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []

    def ioctl(*arguments: object) -> None:
        calls.append(arguments)

    def read(descriptor: int, size: int) -> bytes:
        assert descriptor == 0
        assert size == 4096
        return INPUT_MARKER

    def readable(*_arguments: object) -> tuple[list[int], list[int], list[int]]:
        return [0], [], []

    def execute(arguments: list[str], **kwargs: object) -> CompletedProcess[bytes]:
        assert kwargs == {"check": False, "timeout": 40, "stdout": subprocess.DEVNULL}
        return CompletedProcess(arguments, 7)

    monkeypatch.setattr(host_probe.fcntl, "ioctl", ioctl)
    monkeypatch.setattr(host_probe.tty, "setraw", calls.append)
    monkeypatch.setattr(host_probe.os, "read", read)
    monkeypatch.setattr(host_probe.select, "select", readable)
    monkeypatch.setattr(host_probe.subprocess, "run", execute)
    with pytest.raises(SystemExit) as caught:
        host_probe.main("terminal", ["fixture"])
    assert caught.value.code == 7
    assert json.loads(capsys.readouterr().out) == {"inputs": INPUT_MARKER.hex()}
    assert calls == [(0, host_probe.termios.TIOCSCTTY, 0), 0]


@pytest.mark.parametrize(
    "case",
    [
        (INPUT_MARKER, b"", errno.ENOENT, True, False, "blocked by device namespace"),
        (b"", b"", errno.ENOENT, True, True, "blocked by device namespace"),
        (b"", b"", errno.ENOENT, True, False, "unproven"),
        (INPUT_MARKER, b"", errno.ENOENT, False, False, "unproven"),
        (b"", b"", errno.ENOENT, False, True, "unproven"),
        (INPUT_MARKER, b"", errno.EPERM, True, False, "unproven"),
        (b"", b"", errno.EPERM, True, True, "unproven"),
        (INPUT_MARKER, b"", 0, True, False, "injected"),
        (b"", INPUT_MARKER, errno.ENOENT, True, True, "injected"),
    ],
)
def test_terminal_missing_device_requires_a_control_and_open_refusal(
    case: tuple[bytes, bytes, int, bool, bool, str],
) -> None:
    control, probe, error, missing, kernel, expected = case
    assert (
        terminal_status(
            control,
            probe,
            TerminalAttempt(attempted=False, error=error, missing_linux_tty=missing),
            kernel_blocked=kernel,
        )
        == expected
    )


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize(
    "case",
    [
        ("linux", "/dev/pts/123", errno.ENOENT, "blocked by device namespace"),
        ("linux", "", errno.EPERM, "blocked by kernel"),
        ("linux", "/dev/pts/123", errno.EPERM, "unproven"),
        ("linux", "/dev/tty", errno.ENOENT, "unproven"),
        ("darwin", "/dev/pts/123", errno.ENOENT, "unproven"),
    ],
)
def test_terminal_linux_device_namespace_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: Profile,
    case: tuple[str, str, int, str],
) -> None:
    platform, terminal, error, expected = case

    def observe(
        command: tuple[str, ...],
        _environment: dict[str, str],
        _cwd: Path,
        *,
        prompt: str | None = None,
        tty_file: Path | None = None,
    ) -> TerminalObservation:
        assert isinstance(tty_file, Path)
        if terminal:
            tty_file.write_text(terminal)
        target = Path(command[-1])
        sandbox = target.suffix == ".sh"
        assert (prompt is not None) is sandbox
        witness = Path(shlex.split(target.read_text())[-1]) if sandbox else target
        witness.write_text(
            json.dumps(
                terminal_record(
                    not sandbox or not terminal, error if sandbox else errno.EPERM
                )
            )
        )
        return TerminalObservation(b"", 0)

    module = sys.modules[__name__]
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(module, "observe_terminal", observe)
    monkeypatch.setattr(module, "legacy_input_setting", lambda: "0")
    if expected == "unproven":
        with pytest.raises(pytest.fail.Exception, match="unproven"):
            test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)
    else:
        test_live_terminal_injection(tmp_path, monkeypatch, Harness.CODEX, profile)
        assert expected in capsys.readouterr().out


@pytest.mark.parametrize(
    ("platform", "contents", "expected"),
    [("linux", "0\n", "0"), ("linux", " 1\n", "1"), ("darwin", "", "unavailable")],
)
def test_terminal_linux_setting_reads_the_kernel_switch(
    monkeypatch: pytest.MonkeyPatch, platform: str, contents: str, expected: str
) -> None:
    reads: list[Path] = []

    def read(path: Path) -> str:
        reads.append(path)
        return contents

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "read_text", read)
    assert legacy_input_setting() == expected
    assert reads == (
        [Path("/proc/sys/dev/tty/legacy_tiocsti")] if platform == "linux" else []
    )


@pytest.mark.usefixtures("fake_host_tools")
@pytest.mark.parametrize("profile", Profile)
def test_codex_local_sandbox_uses_the_rendered_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: Profile
) -> None:
    run, paths = live_policy(tmp_path, monkeypatch, Harness.CODEX, profile)
    value, executable = checked_live_policy(run, paths)
    run.files.write("prompt", "fixture")
    command = codex_sandbox_command(
        run, paths, value, executable, "/bin/sh", "-c", "fixture"
    )
    assert command[:3] == (executable, "sandbox", "-c")
    assert command[-4:] == ("--", "/bin/sh", "-c", "fixture")
    assert f'default_permissions="{profile.value}"' in command
    assert (
        sum(word.startswith(f"permissions.{profile.value}=") for word in command) == 1
    )
    assert command.count("-c") == 3
    assert not any(word.startswith("shell_environment_policy=") for word in command)


@live_only
def test_live_signing_key_inside_denied_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, paths = live_policy(tmp_path, monkeypatch, Harness.CODEX, Profile.BUILD)
    directory = tmp_path / "denied-config"
    directory.mkdir()
    key = directory / "agent-signing-key"
    key.write_text("synthetic signing material\n")
    key.chmod(0o600)
    sandbox_settings = dataclasses.replace(
        run.settings.sandbox,
        denied_home_paths=(*run.settings.sandbox.denied_home_paths, str(directory)),
    )
    signing_credentials = tuple(
        dataclasses.replace(c, signing=str(key)) for c in run.settings.credentials
    )
    config = dataclasses.replace(
        run.settings, sandbox=sandbox_settings, credentials=signing_credentials
    )
    run = dataclasses.replace(run, settings=config)
    value, executable = checked_live_policy(run, paths)
    assert str(directory.resolve()) in value.deny
    assert key.read_text() == "synthetic signing material\n"
    assert list(directory.iterdir()) == [key]
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    run.files.write("prompt", "unused")
    rendered = run_host.harness_command(run, prepared, value, executable)
    cat = shlex.quote(host_tools.resolve("cat", value.tool_path))
    listing = shlex.quote(host_tools.resolve("ls", value.tool_path))
    # A successful shell and both recorded statuses prove the sandbox started.
    script = (
        f"{cat} {shlex.quote(str(key))} >/dev/null 2>&1; read_rc=$?; "
        f"{listing} {shlex.quote(str(directory))} >/dev/null 2>&1; list_rc=$?; "
        'printf "%s %s\\n" "$read_rc" "$list_rc"'
    )
    result = subprocess.run(
        run_host.argv.sandbox(rendered, "/bin/sh", "-c", script),
        env=value.env,
        cwd=paths.worktree,
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    read_rc, list_rc = result.stdout.split()
    assert int(read_rc) in {1, 2}
    assert int(list_rc) in {1, 2}
