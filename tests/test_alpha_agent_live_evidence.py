"""Fake runners and host observations keep live probes from claiming false passes."""

from __future__ import annotations

import dataclasses
import shlex
import subprocess
import sys
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
lazy from pathlib import Path

import pytest
import test_alpha_agent_sandbox_live as live
from agent_live_evidence import MIRROR_LABELS, mirror_cases, run_default_pytest
from agent_live_probe import Proxy

from tbhbdev.alpha.agent import credentials, run_host
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import Harness, Invalid

MIRRORS = MIRROR_LABELS
EXTRA = (*MIRRORS, "file-control", "local-binding", "host-loopback")


@pytest.fixture(autouse=True)
def fake_codex_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_auth(_source: Path, _destination: Path) -> None:
        return None

    monkeypatch.setattr(credentials, "auth_copy", no_auth)


def test_host_marker_path_is_common_to_all_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "account"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert run_host.TAINT_DIRECTORY.parts == ("/", "var", "tmp", "tbhbdev-agent-taint")


def test_host_mirror_evidence_detects_each_real_operation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cases = mirror_cases(tmp_path / "fixtures", workspace, sys.executable, "/bin/ln")
    assert " ".join(MIRROR_LABELS) == (
        "mirror-modify mirror-create mirror-replace alternates hard-link"
    )
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


def test_default_suite_runs_harness_free_sandbox_probes(tmp_path: Path) -> None:
    names = (
        "test_local_sandbox_commands_use_the_build_policy",
        "test_socket_listener_records_connections",
        "test_script_records_real_exit_statuses",
        "test_probe_cannot_trust_model_claims",
        "test_proxy_relays_the_control_and_records_direct_connections",
        "test_refusals_require_an_observed_positive_control",
        "test_agy_live_request_reaches_the_policy_refusal",
    )
    result = run_default_pytest(
        tmp_path, "-v", str(Path(live.__file__)), "-k", " or ".join(names)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "11 passed" in result.stdout, result.stdout
    assert "SKIPPED" not in result.stdout


@dataclasses.dataclass
class SocketProbeRunner:
    run: run_host.Run
    control: Proxy
    platform: str
    scripts: list[str] = dataclasses.field(default_factory=list)

    def record(self, label: str) -> None:
        status = int(
            label not in {"api.github.com", "file-control"}
            and not (label == "local-binding" and self.platform == "linux")
        )
        with (self.run.tmpdir / "observed").open("a") as output:
            output.write(f"{label} {status}\n")
        if label == "api.github.com":
            self.control.targets.append("api.github.com:443")
        if label == "file-control":
            (self.run.tmpdir / "write-control").write_text("invoked")

    def __call__(
        self, command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompt = "Execute this exact command with Bash, then finish: "
        text = kwargs["input"]
        assert isinstance(text, str)
        assert text.startswith(prompt)
        script = Path(shlex.split(text.removeprefix(prompt))[-1]).read_text()
        self.scripts.append(script)
        # Fake the CLI ending a complete tool call on the first blocked domain.
        for line in script.splitlines():
            if "https://example.com" in line:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    "",
                    'Network access to "example.com" was blocked: '
                    "domain is not on the allowlist",
                )
            # Linux's isolated network namespace cannot reach host loopback.
            # https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/linux-sandbox/src/proxy_routing.rs
            if self.platform == "linux" and "https://api.github.com" in line:
                assert '--proxy "$HTTPS_PROXY"' in line
                assert "--noproxy ''" in line
            if line.startswith("echo '"):
                self.record(shlex.split(line)[1].split()[0])
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_socket_probe_isolates_network_refusals_and_keeps_every_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run, paths = live.probe_run(tmp_path, Harness.CODEX)
    control = Proxy(1)
    runner = SocketProbeRunner(run, control, platform)

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

    def sockets(_stack: ExitStack) -> dict[str, live.Listener]:
        return {
            label: live.Listener(f"/example/{label}")
            for label in ("TMPDIR", "tmp", "HOME")
        }

    monkeypatch.setattr(run_host, "checked_policy", checked)
    monkeypatch.setattr(live, "scratch_servers", sockets)
    monkeypatch.setattr(live, "control_proxy", lambda: nullcontext(control))
    monkeypatch.setattr(live, "subprocess", SimpleNamespace(run=runner))
    with ExitStack() as stack:
        live.run_probe(run, paths, "/example/codex", stack)
    output = capsys.readouterr().out
    assert "unproven" not in output
    assert all(
        f"codex {label}:" in output
        for label in (
            *EXTRA,
            "TMPDIR",
            "tmp",
            "HOME",
            "api.github.com",
            "example.com",
            "project-hooks",
        )
    )
    assert len(runner.scripts) == 7
    assert "curl" not in runner.scripts[0]
    assert all(
        label in runner.scripts[0]
        for label in (*MIRRORS, "file-control", "local-binding")
    )
    assert all(
        sum(
            domain in script
            for domain in (
                "https://api.github.com",
                "https://example.com",
                "--unix-socket",
                "--proxy ''",
            )
        )
        == 1
        for script in runner.scripts[1:]
    )


@pytest.mark.parametrize(
    "case", ["stderr", "stdout", "other-domain", "other-error", "no-domain", "recorded"]
)
def test_domain_refusal_requires_the_cli_stderr_and_expected_domain(
    tmp_path: Path, case: str
) -> None:
    results = tmp_path / "observed"
    domain = None if case == "no-domain" else "example.com"
    call = live.ProbeCall("example.com", "fixture", domain)
    refusal = (
        'Network access to "example.com" was blocked: domain is not on the allowlist'
    )
    stdout = refusal if case == "stdout" else ""
    stderr = {
        "other-domain": refusal.replace("example.com", "other.example"),
        "other-error": "fixture error",
        "stdout": "",
    }.get(case, refusal)
    if case == "recorded":
        results.write_text("example.com 0\n")
    live.record_domain_refusal(
        call, subprocess.CompletedProcess(["fixture"], 1, stdout, stderr), results
    )
    expected = {"example.com": "refused"} if case == "stderr" else {}
    if case == "recorded":
        expected = {"example.com": "0"}
    assert live.read_observations(results) == expected
