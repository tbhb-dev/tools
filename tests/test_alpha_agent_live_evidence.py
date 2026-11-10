"""Fake runners and host observations keep live probes from claiming false passes."""

from __future__ import annotations

import subprocess
import sys
from contextlib import ExitStack, nullcontext
lazy from pathlib import Path

import pytest
import test_alpha_agent_sandbox_live as live
from agent_live_evidence import MIRROR_LABELS, mirror_cases
from agent_live_probe import Proxy

from tbhbdev.alpha.agent import run_host
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import Harness, Invalid

MIRRORS = (
    "mirror-modify",
    "mirror-create",
    "mirror-replace",
    "alternates",
    "hard-link",
)
EXTRA = (*MIRRORS, "file-control", "local-binding", "host-loopback")


def test_host_mirror_evidence_detects_each_real_operation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cases = mirror_cases(tmp_path / "fixtures", workspace, sys.executable, "/bin/ln")
    assert tuple(case.label for case in cases) == MIRROR_LABELS
    assert len({case.object_file for case in cases}) == len(MIRROR_LABELS)
    for case in cases:
        assert case.unchanged()
        subprocess.run(case.command, check=True, timeout=5)
        assert not case.unchanged(), case.label


@pytest.mark.parametrize("change", ["missing", "symlink"])
def test_host_mirror_evidence_refuses_replaced_sources(
    tmp_path: Path, change: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    case = mirror_cases(tmp_path / "fixtures", workspace, sys.executable, "/bin/ln")[0]
    previous = case.object_file.with_name("previous")
    case.object_file.rename(previous)
    if change == "symlink":
        case.object_file.symlink_to(previous)
    assert not case.unchanged()


def observed(tmp_path: Path) -> tuple[Path, Proxy]:
    results = tmp_path / "observed"
    results.write_text(
        "api.github.com 0\nexample.com 7\n"
        + "".join(
            f"{label} {0 if label in ('mirror-create', 'file-control') else 1}\n"
            for label in EXTRA
        )
    )
    return results, Proxy(1, targets=["api.github.com:443"])


def test_host_mirror_state_outvotes_the_sandbox_exit_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results, control = observed(tmp_path)
    live.assert_observations(
        Harness.CLAUDE,
        {},
        results,
        control,
        evidence=live.Evidence(dict.fromkeys(MIRRORS, True), written=True),
    )
    output = capsys.readouterr().out
    assert "claude mirror-create: refused" in output
    assert all(f"claude {label}:" in output for label in EXTRA)


def test_probe_reports_every_label_after_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results, control = observed(tmp_path)
    with pytest.raises(AssertionError):
        live.assert_observations(
            Harness.CODEX,
            {},
            results,
            control,
            evidence=live.Evidence(dict.fromkeys(MIRRORS, False), written=True),
        )
    output = capsys.readouterr().out
    assert all(f"codex {label}:" in output for label in EXTRA)


def test_failed_write_control_makes_every_label_unproven(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results, control = observed(tmp_path)
    with pytest.raises(AssertionError):
        live.assert_observations(
            Harness.CODEX,
            {},
            results,
            control,
            evidence=live.Evidence(dict.fromkeys(MIRRORS, True), written=False),
        )
    output = capsys.readouterr().out
    assert all(f"codex {label}: unproven" in output for label in EXTRA)


@pytest.mark.parametrize("reason", ["sandbox", "missing", "policy"])
def test_no_live_evidence_fails_instead_of_skipping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_SANDBOX", "test" if reason == "sandbox" else "")

    def installed(_name: str) -> str | None:
        return None if reason == "missing" else "/example/codex"

    def refused(
        _run: run_host.Run,
        _paths: run_host.RunPaths,
        _installed: str,
        _stack: ExitStack,
    ) -> None:
        msg = "policy refused"
        raise Invalid(msg)

    monkeypatch.setattr(live.shutil, "which", installed)
    monkeypatch.setattr(live, "run_probe", refused)
    with pytest.raises(pytest.fail.Exception):
        live.test_build_sandboxes_refuse_user_sockets(tmp_path, Harness.CODEX)
    output = capsys.readouterr().out
    assert all(f"codex {label}: unproven" in output for label in EXTRA)


@pytest.mark.parametrize("status", [0, 1])
def test_runner_that_never_executes_the_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run, paths = live.probe_run(tmp_path, Harness.CODEX)

    def checked(
        actual: run_host.Run, actual_paths: run_host.RunPaths
    ) -> tuple[run_host.Policy, str]:
        value = policy.build(
            actual.settings,
            actual.host,
            paths=actual_paths,
            harness=Harness.CODEX,
            profile=actual.request.profile,
            account="agent",
            unsandboxed=False,
        )
        return value, "/example/codex"

    def no_sockets(_stack: ExitStack) -> dict[str, live.Listener]:
        return {}

    def control() -> nullcontext[Proxy]:
        return nullcontext(Proxy(1))

    def execute(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["codex"], status, "", "not logged in")

    monkeypatch.setattr(run_host, "checked_policy", checked)
    monkeypatch.setattr(live, "scratch_servers", no_sockets)
    monkeypatch.setattr(live, "control_proxy", control)
    monkeypatch.setattr(live.subprocess, "run", execute)
    with ExitStack() as stack, pytest.raises(AssertionError):
        live.run_probe(run, paths, "/example/codex", stack)
    output = capsys.readouterr().out
    assert all(f"codex {label}: unproven" in output for label in EXTRA)
