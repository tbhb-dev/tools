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

from tbhbdev.alpha.agent import credentials, run_host, runfiles
from tbhbdev.alpha.agent.decisions import argv, policy
from tbhbdev.alpha.agent.model import ClaudePolicy, Harness, Invalid, Profile, RunPaths

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import Host, Policy, Settings


def report(line: str) -> None:
    sys.stdout.write(line + "\n")


@dataclasses.dataclass
class Listener:
    path: str
    connections: list[bool] = dataclasses.field(default_factory=list)


def accept_connections(
    server: socket.socket, listener: Listener, stopped: threading.Event
) -> None:
    while not stopped.is_set():
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
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


def probe_script(sockets: dict[str, Listener], curl: str, results: Path) -> str:
    lines = ["umask 077", f": > {shlex.quote(str(results))}"]
    for location, listener in sockets.items():
        command = shlex.join(
            (
                curl,
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
    return "\n".join(lines) + "\n"


def probe_run(tmp_path: Path, harness: Harness) -> tuple[run_host.Run, RunPaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = settings()
    config = dataclasses.replace(
        config,
        sandbox=dataclasses.replace(
            config.sandbox,
            write_roots=(str(tmp_path),),
            agy_unsandboxed_opt_out=None,
            network_domains=("api.github.com",),
        ),
    )
    paths = RunPaths(
        str(Path.home()),
        str(workspace),
        str(tmp_path),
        str(tmp_path / "codex"),
        str(tmp_path / "audit"),
        uid=os.getuid(),
    )
    request = run_request(
        harness=harness.value,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="sandbox probe" if harness is Harness.CLAUDE else None,
    )
    run = run_host.Run(
        runfiles.RunFiles(tmp_path / "runs", "probe"),
        request,
        config,
        LINUX if sys.platform == "linux" else MACOS,
        Path.home(),
        tmp_path,
        0,
        0,
    )
    Path(paths.codex_home).mkdir(mode=0o700)
    return run, paths


def sandbox_command(
    run: run_host.Run, paths: RunPaths, value: Policy, executable: str, script: Path
) -> tuple[str, ...]:
    if value.harness is Harness.CLAUDE:
        prepared = run_host.Prepared(
            paths,
            credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
            None,
        )
        # This is the production builder, including its generated --settings JSON.
        # Claude consumes JSON directly; sandbox-runtime's schema is different.
        return run_host.harness_command(run, prepared, value, executable)
    generated = argv.harness(
        run.request,
        value,
        worktree=paths.worktree,
        brief_file=str(run.files.path("prompt")),
    )
    start = generated.index("--strict-config") + 1
    end = generated.index("-m")
    return (
        executable,
        "sandbox",
        "-P",
        "build",
        *generated[start:end],
        "--",
        "/bin/sh",
        str(script),
    )


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


def assert_observations(
    harness: Harness, sockets: dict[str, Listener], results: Path
) -> None:
    observations = dict(line.split() for line in results.read_text().splitlines())
    assert set(observations) == {*sockets, "api.github.com", "example.com"}
    for location, listener in sockets.items():
        connected = bool(listener.connections)
        report(f"{harness} {location}: {'allowed' if connected else 'refused'}")
        assert not connected, f"{location}: listener accepted a connection"
    for domain, allowed in (("api.github.com", True), ("example.com", False)):
        reached = observations[domain] == "0"
        report(f"{harness} {domain}: {'allowed' if reached else 'refused'}")
        assert reached is allowed, f"{domain}: curl exit {observations[domain]}"


def run_probe(
    run: run_host.Run, paths: RunPaths, installed: str, stack: ExitStack
) -> None:
    harness = run.request.harness
    sockets = scratch_servers(stack)
    results = run.tmpdir / "observed"
    script = run.tmpdir / "probe.sh"
    curl = shutil.which("curl")
    assert curl, "curl is required for live probes"
    script.write_text(probe_script(sockets, curl, results))
    run.files.write(
        "prompt",
        "Execute this exact command with Bash, then finish: "
        f"/bin/sh {shlex.quote(str(script))}",
    )
    value = policy.build(
        run.settings,
        run.host,
        harness=harness,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=paths,
        denied=run_host.resolved_denies(run, paths),
    )
    command = sandbox_command(run, paths, value, installed, script)
    result = subprocess.run(
        command,
        input=run.files.path("prompt").read_text(),
        env=value.env
        | {
            "HOME": str(run.tmpdir),
            "CODEX_HOME": paths.codex_home,
            "CLAUDE_CONFIG_DIR": str(run.tmpdir / "claude"),
        },
        cwd=paths.worktree,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if harness is Harness.CLAUDE and (result.returncode != 0 or not results.is_file()):
        for location in (*sockets, "api.github.com", "example.com"):
            report(f"Claude {location}: unproven by test")
        pytest.skip("Claude: unproven by test")
    assert result.returncode == 0, (
        f"{harness}: sandbox command exit {result.returncode}"
    )
    assert_observations(harness, sockets, results)


@pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="set TBHBDEV_SANDBOX_LIVE=1 on a real host to test sandbox socket refusal",
)
@pytest.mark.parametrize("harness", Harness)
def test_build_sandboxes_refuse_user_sockets(tmp_path: Path, harness: Harness) -> None:
    if os.environ.get("CODEX_SANDBOX"):
        pytest.skip(
            "Claude: unproven by test"
            if harness is Harness.CLAUDE
            else "coordinator must run outside the agent sandbox"
        )
    installed = shutil.which(harness.value)
    if installed is None:
        pytest.skip(
            "Claude: unproven by test"
            if harness is Harness.CLAUDE
            else f"{harness}: harness binary not installed"
        )
    run, paths = probe_run(tmp_path, harness)
    if harness is Harness.AGY:
        report_agy_refusals(run.settings, run.host, paths)
        return
    with ExitStack() as stack:
        run_probe(run, paths, installed, stack)


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
        assert command[1:4] == ("sandbox", "-P", "build")
        assert command[-3:] == ("--", "/bin/sh", str(tmp_path / "probe.sh"))
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
