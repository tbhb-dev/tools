"""Fake runners and host observations keep live probes from claiming false passes."""

from __future__ import annotations

import dataclasses
import hashlib
import shlex
import subprocess
import sys
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from typing import cast
lazy from collections.abc import Callable
lazy from pathlib import Path

import pytest
import test_alpha_agent_sandbox_live as live
from agent_live_evidence import (
    ALL_LABELS,
    MIRROR_LABELS,
    mirror_cases,
    run_default_pytest,
)
from agent_live_probe import Proxy

from tbhbdev.alpha.agent import credentials, run_host
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import Harness, Invalid

MIRRORS = MIRROR_LABELS
EXTRA = (*MIRRORS, "file-control", "local-binding", "host-loopback", "hooks-write")


@dataclasses.dataclass
class ProbeInvocation:
    command: tuple[str, ...] = dataclasses.field(repr=False)
    kwargs: dict[str, object] = dataclasses.field(repr=False)


type ProbeCalls = list[ProbeInvocation]
type TokenProbe = tuple[run_host.Run, run_host.RunPaths, ProbeCalls]


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
    attempts: list[bool] = []

    def installed(_name: str) -> str | None:
        return None if reason == "missing" else "/example/codex"

    def refused(
        _run: run_host.Run,
        _paths: run_host.RunPaths,
        _installed: str,
        _stack: ExitStack,
    ) -> None:
        attempts.append(True)
        msg = "policy refused"
        raise Invalid(msg)

    monkeypatch.setattr(live.shutil, "which", installed)
    monkeypatch.setattr(live, "run_probe", refused)
    with pytest.raises(pytest.fail.Exception) as error:
        live.test_build_sandboxes_refuse_user_sockets(tmp_path, Harness.CODEX)
    assert (
        str(error.value)
        == {
            "sandbox": "coordinator must run outside the agent sandbox",
            "missing": "codex: harness binary not installed",
            "policy": "codex: no sandbox evidence (Invalid)",
        }[reason]
    )
    assert bool(attempts) is (reason == "policy")
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


def test_live_socket_probe_collects_both_harnesses(tmp_path: Path) -> None:
    result = run_default_pytest(
        tmp_path,
        "-v",
        str(Path(live.__file__)),
        "-k",
        "test_build_sandboxes_refuse_user_sockets",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for harness in (Harness.CODEX, Harness.CLAUDE):
        case = f"test_build_sandboxes_refuse_user_sockets[{harness}] SKIPPED"
        assert case in result.stdout, result.stdout
    assert "2 skipped" in result.stdout, result.stdout


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


@pytest.fixture(autouse=True)
def no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(credentials.CLAUDE_TOKEN_ENV, raising=False)


@pytest.fixture
def token_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> TokenProbe:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TBHBDEV_LIVE_CLAUDE_TOKEN_FILE", raising=False)
    run, paths = live.probe_run(tmp_path, harness)
    control = Proxy(1)
    runner = SocketProbeRunner(run, control, sys.platform)
    calls: ProbeCalls = []
    mirrors: list[live.MirrorProbe] = []
    value = policy.build(
        run.settings,
        run.host,
        paths=paths,
        harness=harness,
        profile=run.request.profile,
        account="agent",
        unsandboxed=False,
    )

    def checked(
        actual: run_host.Run, actual_paths: run_host.RunPaths
    ) -> tuple[run_host.Policy, str]:
        assert actual_paths == paths
        assert actual.settings.sandbox.denied_home_paths == (
            *run.settings.sandbox.denied_home_paths,
            *(str(case.object_file.parents[2]) for case in mirrors),
        )
        return value, "/example/harness"

    def sockets(stack: ExitStack) -> dict[str, live.Listener]:
        assert isinstance(stack, ExitStack)
        return {}

    def directory(**kwargs: object) -> nullcontext[str]:
        assert kwargs == {"prefix": "ag-probe-"}
        return nullcontext(str(root))

    def cases(
        directory: Path, workspace: Path, python: str, ln: str
    ) -> tuple[live.MirrorProbe, ...]:
        assert python == sys.executable
        assert ln == live.shutil.which("ln")
        result = mirror_cases(directory, workspace, python, ln)
        mirrors.extend(result)
        return result

    original_calls = live.probe_calls

    def probes(
        sockets: dict[str, live.Listener],
        curl: str,
        results: Path,
        extra: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> tuple[live.ProbeCall, ...]:
        assert curl == live.shutil.which("curl")
        assert results == run.tmpdir / "observed"
        assert extra[:-1] == live.live_extra(
            tuple(mirrors), run.tmpdir / "write-control", curl, control
        )
        return original_calls(sockets, curl, results, extra)

    original_command = live.sandbox_command

    def sandbox(
        actual: run_host.Run,
        actual_paths: run_host.RunPaths,
        actual_value: run_host.Policy,
        installed: str,
        script: Path,
    ) -> tuple[str, ...]:
        assert script.is_file()
        return original_command(actual, actual_paths, actual_value, installed, script)

    def execute(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(ProbeInvocation(command, kwargs))
        assert kwargs["cwd"] == paths.worktree
        assert kwargs["text"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 120
        environment = cast("dict[str, object]", kwargs["env"])
        assert isinstance(environment, dict)
        expected = (
            value.env
            | {"CODEX_HOME": paths.codex_home}
            | dict.fromkeys(
                ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"), control.url
            )
        )
        matches = {
            key: val
            for key, val in environment.items()
            if key != credentials.CLAUDE_TOKEN_ENV
        } == expected
        assert matches
        return runner(command, **kwargs)

    root = tmp_path / "scripts"
    root.mkdir()
    monkeypatch.setattr(run_host, "checked_policy", checked)
    monkeypatch.setattr(live, "scratch_servers", sockets)
    monkeypatch.setattr(live, "control_proxy", lambda: nullcontext(control))
    monkeypatch.setattr(
        live,
        "tempfile",
        SimpleNamespace(TemporaryDirectory=directory),
    )
    monkeypatch.setattr(
        live,
        "subprocess",
        SimpleNamespace(run=execute, TimeoutExpired=subprocess.TimeoutExpired),
    )
    monkeypatch.setattr(live, "mirror_cases", cases)
    monkeypatch.setattr(live, "probe_calls", probes)
    monkeypatch.setattr(live, "sandbox_command", sandbox)
    return run, paths, calls


FILE_VARIABLE = "TBHBDEV_LIVE_CLAUDE_TOKEN_FILE"
FAKE_TOKEN = "example-token-value"  # noqa: S105


def fingerprint(value: str) -> str:
    """A digest, so a failed assert never prints the fake token."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def fingerprints(environment: dict[str, str]) -> dict[str, str]:
    return {key: fingerprint(value) for key, value in environment.items()}


@pytest.mark.parametrize("harness", [Harness.CLAUDE, Harness.CODEX])
@pytest.mark.parametrize("configured", [True, False], ids=["set", "unset"])
def test_probe_token_environment_only_reaches_the_claude_calls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    token_probe: TokenProbe,
    harness: Harness,
    configured: bool,
) -> None:
    run, paths, calls = token_probe
    tmp_path = run.tmpdir.parent
    if configured:
        monkeypatch.setenv(credentials.CLAUDE_TOKEN_ENV, f" {FAKE_TOKEN} \t\n\n")
    carried = configured and harness is Harness.CLAUDE
    with ExitStack() as stack:
        live.run_probe(run, paths, "/example/harness", stack)
        assert calls
        for call in calls:
            environment = cast("dict[str, str]", call.kwargs["env"])
            assert (credentials.CLAUDE_TOKEN_ENV in environment) is carried
            sent = fingerprints(
                {
                    key: value
                    for key, value in environment.items()
                    if key == credentials.CLAUDE_TOKEN_ENV
                }
            )
            assert sent == (
                {credentials.CLAUDE_TOKEN_ENV: fingerprint(FAKE_TOKEN)}
                if carried
                else {}
            )
            assert not [i for i, arg in enumerate(call.command) if FAKE_TOKEN in arg]
        assert FAKE_TOKEN not in repr(calls)
        captured = capsys.readouterr()
        assert not [i for i, text in enumerate(captured) if FAKE_TOKEN in text]
        assert (run.tmpdir / "observed").is_file()
        assert not [
            str(path)
            for path in tmp_path.rglob("*")
            if path.is_file() and FAKE_TOKEN.encode() in path.read_bytes()
        ]


@pytest.mark.parametrize("harness", [Harness.CLAUDE, Harness.CODEX])
@pytest.mark.parametrize(
    "value", ["", " \t\n", f" {FAKE_TOKEN} \n"], ids=["empty", "blank", "token"]
)
def test_probe_token_environment_strips_and_skips_blank(
    monkeypatch: pytest.MonkeyPatch, harness: Harness, value: str
) -> None:
    monkeypatch.delenv(FILE_VARIABLE, raising=False)
    monkeypatch.setenv(credentials.CLAUDE_TOKEN_ENV, value)
    expected = (
        {credentials.CLAUDE_TOKEN_ENV: fingerprint(FAKE_TOKEN)}
        if value.strip() and harness is Harness.CLAUDE
        else {}
    )
    assert fingerprints(live.probe_token_env(harness)) == expected


@pytest.mark.parametrize("with_environment", [True, False], ids=["env", "no-env"])
def test_probe_token_file_variable_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_environment: bool,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(f"{FAKE_TOKEN}\n")
    monkeypatch.setenv(FILE_VARIABLE, str(token_file))
    if with_environment:
        monkeypatch.setenv(credentials.CLAUDE_TOKEN_ENV, FAKE_TOKEN)
    with pytest.raises(ValueError, match=FILE_VARIABLE) as error:
        live.probe_token_env(Harness.CLAUDE)
    message = str(error.value)
    assert credentials.CLAUDE_TOKEN_ENV in message
    assert str(token_file) not in message
    assert FAKE_TOKEN not in message


def test_probe_token_file_variable_does_not_affect_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FILE_VARIABLE, str(tmp_path / "token"))
    assert live.probe_token_env(Harness.CODEX) == {}


@pytest.mark.parametrize("harness", [Harness.CLAUDE])
def test_run_probe_refuses_the_token_file_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    token_probe: TokenProbe,
) -> None:
    run, paths, calls = token_probe
    monkeypatch.setenv(FILE_VARIABLE, str(run.tmpdir.parent / "token"))
    monkeypatch.setenv(credentials.CLAUDE_TOKEN_ENV, FAKE_TOKEN)
    with (
        ExitStack() as stack,
        pytest.raises(ValueError, match=FILE_VARIABLE) as error,
    ):
        live.run_probe(run, paths, "/example/harness", stack)
    assert FAKE_TOKEN not in str(error.value)
    assert not [i for i, text in enumerate(capsys.readouterr()) if FAKE_TOKEN in text]
    assert not calls


@pytest.mark.parametrize("harness", [Harness.CLAUDE])
def test_live_entry_preserves_the_token_file_refusal(
    monkeypatch: pytest.MonkeyPatch, token_probe: TokenProbe
) -> None:
    run, paths, _calls = token_probe
    monkeypatch.setenv(FILE_VARIABLE, str(run.tmpdir.parent / "token"))
    monkeypatch.setenv("CODEX_SANDBOX", "")
    original = live.shutil.which

    def installed(name: str) -> str | None:
        return "/example/claude" if name == "claude" else original(name)

    def prepared(
        _root: Path, _harness: Harness
    ) -> tuple[run_host.Run, run_host.RunPaths]:
        return run, paths

    monkeypatch.setattr(live.shutil, "which", installed)
    monkeypatch.setattr(live, "probe_run", prepared)
    with pytest.raises(ValueError, match=FILE_VARIABLE) as error:
        live.test_build_sandboxes_refuse_user_sockets(run.tmpdir.parent, Harness.CLAUDE)
    assert credentials.CLAUDE_TOKEN_ENV in str(error.value)


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_live_entry_runs_the_selected_harness(
    monkeypatch: pytest.MonkeyPatch, token_probe: TokenProbe, harness: Harness
) -> None:
    run, paths, calls = token_probe
    monkeypatch.setenv("CODEX_SANDBOX", "")
    original = live.shutil.which
    lookups: list[str] = []
    auth_calls: list[tuple[object, ...]] = []

    def installed(name: str) -> str | None:
        lookups.append(name)
        return "/example/harness" if name == harness.value else original(name)

    def prepared(
        root: Path, actual_harness: Harness
    ) -> tuple[run_host.Run, run_host.RunPaths]:
        assert root == run.tmpdir.parent
        assert actual_harness is harness
        assert run.request.claude_profile == (
            "primary" if harness is Harness.CLAUDE else None
        )
        return run, paths

    def auth(*args: object) -> nullcontext[None]:
        auth_calls.append(args)
        return nullcontext(None)

    original_probe = live.run_probe

    def probe(
        actual: run_host.Run,
        actual_paths: run_host.RunPaths,
        executable: str,
        stack: ExitStack,
    ) -> None:
        assert actual.settings.sandbox.host_path == (
            *run.settings.sandbox.host_path,
            "/example",
        )
        assert actual_paths == paths
        assert executable == "/example/harness"
        original_probe(actual, actual_paths, executable, stack)

    monkeypatch.setattr(live.shutil, "which", installed)
    monkeypatch.setattr(live, "probe_run", prepared)
    monkeypatch.setattr(live, "codex_auth", auth)
    monkeypatch.setattr(live, "run_probe", probe)
    live.test_build_sandboxes_refuse_user_sockets(run.tmpdir.parent, harness)
    assert lookups[0] == harness.value
    assert len(calls) == 4
    assert all(call.command[0] == "/example/harness" for call in calls)
    assert bool(auth_calls) is (harness is Harness.CODEX)


def test_agy_live_entry_reports_the_policy_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    original = live.report_agy_refusals

    def report(
        config: run_host.Settings, host: run_host.Host, paths: run_host.RunPaths
    ) -> None:
        assert isinstance(paths, run_host.RunPaths)
        original(config, host, paths)

    monkeypatch.setattr(live, "report_agy_refusals", report)
    live.test_build_sandboxes_refuse_user_sockets(tmp_path, Harness.AGY)
    assert capsys.readouterr().out == (
        "agy build: refused by policy\nagy review: refused by policy\n"
    )


@pytest.mark.parametrize("harness", [Harness.CLAUDE])
@pytest.mark.parametrize("failure", ["ln", "curl", "write-control", "hook-ran"])
def test_token_probe_requires_tools_and_observed_controls(
    monkeypatch: pytest.MonkeyPatch, token_probe: TokenProbe, failure: str
) -> None:
    run, paths, _calls = token_probe
    if failure in {"ln", "curl"}:
        original = live.shutil.which

        def installed(name: str) -> str | None:
            return None if name == failure else original(name)

        monkeypatch.setattr(live.shutil, "which", installed)
        message = (
            "ln is required for hard-link probes"
            if failure == "ln"
            else "curl is required for live probes"
        )
    else:
        original_runner = cast(
            "Callable[..., subprocess.CompletedProcess[str]]", live.subprocess.run
        )

        def execute(
            command: tuple[str, ...], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            result = original_runner(command, **kwargs)
            if failure == "write-control":
                (run.tmpdir / failure).write_text("failed")
            else:
                (run.tmpdir.parent / "scripts" / failure).touch()
            return result

        monkeypatch.setattr(live, "subprocess", SimpleNamespace(run=execute))
        message = (
            "interpreter write control was not observed"
            if failure == "write-control"
            else "project-hooks: allowed"
        )
    pattern = f"^{message}(?:\n|$)" if failure in {"ln", "curl"} else message
    with ExitStack() as stack, pytest.raises(AssertionError, match=pattern):
        live.run_probe(run, paths, "/example/harness", stack)


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
    assert all(f"codex {label}:" in output for label in ALL_LABELS)
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


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("modify", [False, True])
def test_hooks_write_probe_requires_both_operations_to_fail(
    tmp_path: Path, create: bool, modify: bool
) -> None:
    command = live.hooks_write_probe(tmp_path)
    hooks = tmp_path / ".git/hooks"
    existing = hooks / "probe-existing"
    if create:
        hooks.chmod(0o755)
    if modify:
        existing.chmod(0o644)
    result = subprocess.run(command, cwd=tmp_path, check=False, timeout=5)
    assert result.returncode == (0 if create or modify else 1)
    assert (hooks / "probe-new").exists() is create
    assert existing.read_text() == ("original\nchanged" if modify else "original\n")
