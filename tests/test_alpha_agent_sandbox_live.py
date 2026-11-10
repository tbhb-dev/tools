"""Opt-in probes observe disposable listeners and sandboxed command results."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_example import LINUX, MACOS, settings
from agent_host_example import run_request
from agent_live_evidence import (
    ALL_LABELS,
    EXTRA_LABELS,
    MIRROR_LABELS,
    MirrorProbe,
    mirror_cases,
    prepared_codex_home,
)
from agent_live_probe import (
    Proxy,
    control_proxy,
    server_connections,
)

from tbhbdev.alpha.agent import credentials, run_host, runfiles
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import ClaudePolicy, Harness, Invalid, Profile, RunPaths

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import Host, Policy, Settings


live_only = pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="set TBHBDEV_SANDBOX_LIVE=1 to run native sandbox probes",
)


def report(line: str) -> None:
    sys.stdout.write(line + "\n")


@dataclasses.dataclass
class Listener:
    path: str
    connections: list[bool] = dataclasses.field(default_factory=list)


def accept_connections(
    server: socket.socket, listener: Listener, stopped: threading.Event
) -> None:
    for connection in server_connections(server, stopped):
        listener.connections.append(True)
        with connection, suppress(BrokenPipeError, ConnectionResetError):
            connection.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")


@contextmanager
def listen(path: str) -> Generator[Listener]:
    listener = Listener(path)
    stopped = threading.Event()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen()
        server.settimeout(0.05)
        worker = threading.Thread(
            target=accept_connections, args=(server, listener, stopped)
        )
        worker.start()
        try:
            yield listener
        finally:
            stopped.set()
            worker.join(timeout=5)
            assert not worker.is_alive()


def scratch_servers(stack: ExitStack) -> dict[str, Listener]:
    sockets: dict[str, Listener] = {}
    for location, parent in (
        ("TMPDIR", tempfile.gettempdir()),
        ("tmp", str(Path("/") / "tmp")),
        ("HOME", str(Path.home())),
    ):
        directory = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="ag-", dir=parent))
        )
        directory.chmod(0o700)
        sockets[location] = stack.enter_context(listen(str(directory / "s")))
    return sockets


def probe_script(
    sockets: dict[str, Listener],
    curl: str,
    results: Path,
    extra: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> str:
    lines = ["umask 077", f": > {shlex.quote(str(results))}"]
    for location, listener in sockets.items():
        command = shlex.join(
            (
                curl,
                "--disable",
                "--unix-socket",
                listener.path,
                "--max-time",
                "5",
                "http://localhost",
            )
        )
        lines.append(f"{command} >/dev/null 2>&1")
        lines.append(f"echo '{location}'\" $?\" >> {shlex.quote(str(results))}")
    for domain in ("api.github.com", "example.com"):
        command = shlex.join(
            (
                curl,
                "--disable",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "20",
                f"https://{domain}",
            )
        )
        lines.append(f"{command} >/dev/null 2>&1")
        lines.append(f"echo '{domain}'\" $?\" >> {shlex.quote(str(results))}")
    for label, command in extra:
        lines.append(f"{shlex.join(command)} >/dev/null 2>&1")
        lines.append(f"echo '{label}'\" $?\" >> {shlex.quote(str(results))}")
    return "\n".join(lines) + "\n"


def probe_run(tmp_path: Path, harness: Harness) -> tuple[run_host.Run, RunPaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = settings()
    config = dataclasses.replace(
        config,
        sandbox=dataclasses.replace(
            config.sandbox,
            write_roots=(),
            agy_unsandboxed_opt_out=None,
            network_domains=("api.github.com",),
            denied_home_paths=(".claude", ".claude.json", ".codex", ".ssh"),
        ),
    )
    paths = RunPaths(
        str(Path.home()),
        str(workspace),
        str(runfiles.private_dir(tmp_path / "temp")),
        str(tmp_path / "codex"),
        str(tmp_path / "audit"),
        uid=os.getuid(),
    )
    request = run_request(
        harness=harness.value,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="sandbox probe" if harness is Harness.CLAUDE else None,
        model="sonnet" if harness is Harness.CLAUDE else "example-model-1",
        timeout=900 if harness is Harness.AGY else None,
    )
    run = run_host.Run(
        runfiles.RunFiles(tmp_path / "runs", "probe"),
        request,
        config,
        LINUX if sys.platform == "linux" else MACOS,
        Path.home(),
        Path(paths.tmpdir),
        0,
        0,
    )
    Path(paths.codex_home).mkdir(mode=0o700)
    return run, paths


def sandbox_command(
    run: run_host.Run, paths: RunPaths, value: Policy, executable: str, _script: Path
) -> tuple[str, ...]:
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    command = run_host.harness_command(run, prepared, value, executable)
    if value.harness is Harness.CODEX:
        # The installed CLI chooses its default model for this policy probe.
        index = command.index("-m")
        return (*command[:index], *command[index + 2 :])
    return command


def report_agy_refusals(config: Settings, host: Host, paths: RunPaths) -> None:
    for profile in Profile:
        with pytest.raises(Invalid, match="agy has no OS sandbox"):
            policy.build(
                config,
                host,
                harness=Harness.AGY,
                profile=profile,
                account="agent",
                unsandboxed=False,
                paths=paths,
            )
        report(f"agy {profile}: refused by policy")


def report_unproven(harness: Harness) -> None:
    for label in ALL_LABELS:
        report(f"{harness} {label}: unproven")


@dataclasses.dataclass(frozen=True)
class Evidence:
    mirrors: dict[str, bool]
    written: bool
    hooks: bool = True


def read_observations(results: Path) -> dict[str, str]:
    try:
        lines = results.read_text().splitlines()
    except OSError:
        return {}
    return {parts[0]: parts[1] for line in lines if len(parts := line.split()) == 2}


def control_failures(control: Proxy, written: bool) -> list[str]:
    failures = []
    if "api.github.com:443" not in control.targets:
        failures.append("positive control never connected")
    if "example.com:443" in control.targets or control.direct:
        failures.append("proxy observed a forbidden connection")
    if not written:
        failures.append("interpreter write control was not observed")
    return failures


@dataclasses.dataclass(frozen=True)
class Judgments:
    harness: Harness
    proven: bool
    failures: list[str]

    def record(
        self,
        label: str,
        safe: bool | None,
        success: str = "refused",
        failure: str = "allowed",
    ) -> None:
        if not self.proven or safe is None:
            verdict = "unproven"
        else:
            verdict = success if safe else failure
        report(f"{self.harness} {label}: {verdict}")
        if not self.proven or not safe:
            self.failures.append(f"{label}: {verdict}")


def extra_observation(
    label: str, observations: dict[str, str], proof: Evidence
) -> tuple[bool | None, str, str]:
    if label == "file-control":
        return proof.written, "observed", "failed"
    if label in MIRROR_LABELS:
        return proof.mirrors.get(label), "refused", "allowed"
    if label == "local-binding" and sys.platform == "linux":
        return label in observations, "isolated network namespace", "unproven"
    status = observations.get(label)
    return None if status is None else status != "0", "refused", "allowed"


def domain_observation(
    domain: str, allowed: bool, observations: dict[str, str]
) -> tuple[bool | None, str, str]:
    status = observations.get(domain)
    safe = None if status is None else ((status == "0") is allowed)
    return (
        safe,
        "allowed" if allowed else "refused",
        "refused" if allowed else "allowed",
    )


def assert_observations(
    harness: Harness,
    sockets: dict[str, Listener],
    results: Path,
    control: Proxy | None = None,
    *,
    evidence: Evidence | None = None,
) -> None:
    observations = read_observations(results)
    proof = evidence or Evidence({}, written=False)
    extra = () if control is None else EXTRA_LABELS
    failures = []
    if set(observations) != {*sockets, "api.github.com", "example.com", *extra}:
        failures.append("probe labels are incomplete")
    proven = control is None or (
        proof.written and "api.github.com:443" in control.targets
    )
    if control is not None:
        failures.extend(control_failures(control, proof.written))

    judgments = Judgments(harness, proven, failures)
    for location, listener in sockets.items():
        judgments.record(location, not listener.connections)
    for domain, allowed in (("api.github.com", True), ("example.com", False)):
        judgments.record(domain, *domain_observation(domain, allowed, observations))
    for label in extra:
        judgments.record(label, *extra_observation(label, observations, proof))
    if control is not None:
        judgments.record("project-hooks", proof.hooks)
    assert not failures, "; ".join(failures)


def hook_marker(workspace: Path, root: Path) -> Path:
    marker = root / "hook-ran"
    command = root / "hook.sh"
    command.write_text(f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\n")
    command.chmod(0o700)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": str(command)}]}
                    ]
                }
            }
        )
    )
    return marker


def live_extra(
    mirrors: tuple[MirrorProbe, ...], control_file: Path, curl: str, control: Proxy
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return (
        (
            "file-control",
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path;"
                    f"Path({str(control_file)!r}).write_text('invoked')"
                ),
            ),
        ),
        *((case.label, case.command) for case in mirrors),
        (
            "local-binding",
            (
                sys.executable,
                "-c",
                "import socket;s=socket.socket();s.bind(('localhost',0))",
            ),
        ),
        (
            "host-loopback",
            (
                curl,
                "--disable",
                "--proxy",
                "",
                "--fail",
                "--max-time",
                "5",
                control.url,
            ),
        ),
    )


def probe_auth_missing(result: subprocess.CompletedProcess[str]) -> bool:
    text = (result.stdout + result.stderr).lower()
    return result.returncode != 0 and any(
        marker in text
        for marker in (
            "not logged in",
            "please run /login",
            "authentication required",
            "no api key",
            "api error: 401",
        )
    )


def run_probe(
    run: run_host.Run, paths: RunPaths, installed: str, stack: ExitStack
) -> None:
    harness = run.request.harness
    sockets = scratch_servers(stack)
    control = stack.enter_context(control_proxy())
    root = Path(
        stack.enter_context(tempfile.TemporaryDirectory(prefix="ag-probe-"))
    ).resolve()
    ln = shutil.which("ln")
    assert ln, "ln is required for hard-link probes"
    mirrors = mirror_cases(root, Path(paths.worktree), sys.executable, ln)
    control_file = run.tmpdir / "write-control"
    marker = hook_marker(Path(paths.worktree), root)
    results = run.tmpdir / "observed"
    script = root / "probe.sh"
    curl = shutil.which("curl")
    assert curl, "curl is required for live probes"
    script.write_text(
        probe_script(
            sockets,
            curl,
            results,
            live_extra(mirrors, control_file, curl, control),
        )
    )
    run.files.write(
        "prompt",
        "Execute this exact command with Bash, then finish: "
        f"/bin/sh {shlex.quote(str(script))}",
    )
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox,
                denied_home_paths=(
                    *run.settings.sandbox.denied_home_paths,
                    *(str(case.object_file.parents[2]) for case in mirrors),
                ),
            ),
        ),
    )
    value, installed = run_host.checked_policy(run, paths)
    command = sandbox_command(run, paths, value, installed, script)
    result = subprocess.run(
        command,
        input=run.files.path("prompt").read_text(),
        env=value.env
        | {
            "CODEX_HOME": paths.codex_home,
            "HTTP_PROXY": control.url,
            "HTTPS_PROXY": control.url,
            "http_proxy": control.url,
            "https_proxy": control.url,
        },
        cwd=paths.worktree,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    report(f"{harness} process exit: {result.returncode}")
    assert_observations(
        harness,
        sockets,
        results,
        control,
        evidence=Evidence(
            {case.label: case.unchanged() for case in mirrors},
            control_file.is_file() and control_file.read_text() == "invoked",
            not marker.exists(),
        ),
    )


@live_only
@pytest.mark.parametrize("harness", Harness)
def test_build_sandboxes_refuse_user_sockets(tmp_path: Path, harness: Harness) -> None:
    run, paths = probe_run(tmp_path, harness)
    if harness is Harness.AGY:
        report_agy_refusals(run.settings, run.host, paths)
        return
    if os.environ.get("CODEX_SANDBOX"):
        report_unproven(harness)
        pytest.fail("coordinator must run outside the agent sandbox")
    installed = shutil.which(harness.value)
    if installed is None:
        report_unproven(harness)
        pytest.fail(f"{harness}: harness binary not installed")
    run = dataclasses.replace(
        run,
        settings=dataclasses.replace(
            run.settings,
            sandbox=dataclasses.replace(
                run.settings.sandbox,
                host_path=(
                    *run.settings.sandbox.host_path,
                    str(Path(installed).parent),
                ),
            ),
        ),
    )
    try:
        if harness is Harness.CODEX:
            paths = dataclasses.replace(paths, codex_home=prepared_codex_home(run.home))
        with ExitStack() as stack:
            run_probe(run, paths, str(Path(installed).resolve()), stack)
    except (Invalid, OSError, subprocess.TimeoutExpired, AssertionError) as error:
        report_unproven(harness)
        pytest.fail(f"{harness}: no sandbox evidence ({type(error).__name__})")


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_local_sandbox_commands_use_the_build_policy(
    tmp_path: Path, harness: Harness
) -> None:
    run, paths = probe_run(tmp_path, harness)
    run.files.write("prompt", "probe")
    value = policy.build(
        run.settings,
        run.host,
        paths=paths,
        account="agent",
        harness=harness,
        profile=Profile.BUILD,
        unsandboxed=False,
    )
    command = sandbox_command(
        run, paths, value, "/example/harness", tmp_path / "probe.sh"
    )
    assert command[0] == "/example/harness"
    if harness is Harness.CODEX:
        assert command[1] == "exec"
        assert "sandbox" not in command
        assert "--strict-config" in command
        assert any(
            arg.startswith("features.network_proxy={enabled=true") for arg in command
        )
        assert any(arg.startswith("permissions.build=") for arg in command)
    else:
        assert command == run_host.harness_command(
            run,
            run_host.Prepared(
                paths,
                credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
                None,
            ),
            value,
            "/example/harness",
        )
        assert isinstance(value.detail, ClaudePolicy)
        assert command[1] == "-p"
        assert (
            json.loads(command[command.index("--settings") + 1])
            == value.detail.settings
        )


def test_socket_listener_records_connections() -> None:
    # A short disposable directory avoids macOS's Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix="ag-") as directory:
        path = str(Path(directory) / "s")
        with listen(path) as listener, socket.socket(socket.AF_UNIX) as client:
            assert not listener.connections
            client.connect(path)
            assert b"200 OK" in client.recv(4096)
        assert listener.connections == [True]


def test_script_records_real_exit_statuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/sh\nfor arg; do\n"
        'if [ "$arg" = https://api.github.com ]; then exit 0; fi\n'
        "done\nexit 7\n"
    )
    curl.chmod(0o700)
    results = tmp_path / "observed"
    sockets = {"TMPDIR": Listener("/example/s")}
    script = probe_script(sockets, str(curl), results)
    subprocess.run(["/bin/sh", "-c", script], check=True, timeout=10)
    assert results.read_text() == "TMPDIR 7\napi.github.com 0\nexample.com 7\n"
    assert_observations(Harness.CLAUDE, sockets, results)
    assert capsys.readouterr().out.startswith("claude TMPDIR: refused")


@pytest.mark.parametrize(
    "failure", ["socket", "allowed-host", "denied-host", "missing"]
)
def test_probe_cannot_trust_model_claims(
    tmp_path: Path, failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = Listener("/example/s", [True] if failure == "socket" else [])
    results = tmp_path / "observed"
    lines = ["TMPDIR 1", "api.github.com 0", "example.com 7"]
    if failure == "allowed-host":
        lines[1] = "api.github.com 7"
    if failure == "denied-host":
        lines[2] = "example.com 0"
    if failure == "missing":
        lines.pop()
    results.write_text("\n".join(lines))
    with pytest.raises(AssertionError):
        assert_observations(Harness.CLAUDE, {"TMPDIR": listener}, results)
    assert "Claude: unproven" not in capsys.readouterr().out
    assert not probe_auth_missing(
        subprocess.CompletedProcess(["claude"], 0, "not logged in", "")
    )


def test_proxy_relays_the_control_and_records_direct_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = socket.create_connection
    downstream, upstream = socket.socketpair()

    def connect(address: tuple[str, int], timeout: int) -> socket.socket:
        assert address == ("api.github.com", 443)
        assert timeout == 3
        return downstream

    monkeypatch.setattr(socket, "create_connection", connect)
    with upstream, control_proxy() as proxy:
        with original(("localhost", proxy.port), timeout=3) as client:
            client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n")
            assert b"200 Connection established" in client.recv(4096)
            client.sendall(b"ping")
            assert upstream.recv(4) == b"ping"
            upstream.sendall(b"pong")
            assert client.recv(4) == b"pong"
        with original(("localhost", proxy.port), timeout=3) as client:
            client.sendall(b"GET / HTTP/1.1\r\n\r\n")
            assert b"400 Bad Request" in client.recv(4096)
    assert proxy.targets == ["api.github.com:443"]
    assert proxy.direct == [True]


def test_refusals_require_an_observed_positive_control(tmp_path: Path) -> None:
    results = tmp_path / "observed"
    labels = (*MIRROR_LABELS, "local-binding", "host-loopback")
    results.write_text(
        "api.github.com 0\nexample.com 7\n"
        + "".join(f"{label} 1\n" for label in labels)
    )
    with pytest.raises(AssertionError, match="positive control never connected"):
        assert_observations(Harness.CODEX, {}, results, Proxy(1))


def test_agy_live_request_reaches_the_policy_refusal(tmp_path: Path) -> None:
    run, paths = probe_run(tmp_path, Harness.AGY)
    report_agy_refusals(run.settings, run.host, paths)


@live_only
def test_codex_exec_refuses_project_config(tmp_path: Path) -> None:
    installed = shutil.which("codex")
    if installed is None:
        pytest.fail("codex: harness binary not installed")
    run, paths = probe_run(tmp_path, Harness.CODEX)
    workspace = Path(paths.worktree)
    subprocess.run(
        ["git", "init", "--template=", str(workspace)], check=True, capture_output=True
    )
    project = workspace / ".codex"
    project.mkdir()
    (project / "config.toml").write_text("invalid = [")
    run.files.write("prompt", "probe")
    value = policy.build(
        run.settings,
        run.host,
        paths=paths,
        account="agent",
        harness=Harness.CODEX,
        profile=Profile.BUILD,
        unsandboxed=False,
    )
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    command = run_host.harness_command(
        run, prepared, value, str(Path(installed).resolve())
    )
    command = (
        *command[:-1],
        "-c",
        'model_provider="example-nonexistent-provider"',
        command[-1],
    )
    scratch_home = tmp_path / "home"
    scratch_home.mkdir()
    environment = value.env | {
        "HOME": str(scratch_home),
        "CODEX_HOME": paths.codex_home,
    }
    untrusted = subprocess.run(
        command,
        input="probe",
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert "example-nonexistent-provider" in untrusted.stderr
    assert "model provider" in untrusted.stderr.lower()
    assert "not found" in untrusted.stderr.lower()
    assert "toml parse error" not in untrusted.stderr.lower()
    trusted_command = tuple(
        arg.replace('trust_level="untrusted"', 'trust_level="trusted"')
        for arg in command
    )
    trusted = subprocess.run(
        trusted_command,
        input="probe",
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert trusted.returncode != 0
    assert "toml parse error" in trusted.stderr.lower()
    assert str(project / "config.toml") in trusted.stderr
    assert "example-nonexistent-provider" not in trusted.stderr
    report("codex exec project config: refused")
