"""Opt-in probes use only disposable servers on sockets they created."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_example import LINUX, MACOS, settings
from agent_host_example import run_request

from tbhbdev.alpha.agent import run_host, runfiles
from tbhbdev.alpha.agent.decisions import argv, policy
from tbhbdev.alpha.agent.model import (
    ClaudePolicy,
    Harness,
    Invalid,
    Profile,
    RunPaths,
)

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Policy


def report(line: str) -> None:
    sys.stdout.write(line + "\n")


def probe_script(sockets: dict[str, str], tmux: str, curl: str) -> str:
    lines = ["failed=0"]
    for location, path in sockets.items():
        command = shlex.join((tmux, "-S", path, "list-sessions"))
        lines.append(
            f"if {command} >/dev/null 2>&1; then "
            f"echo '{location}: CONNECTED'; failed=1; "
            f"else echo '{location}: refused'; fi"
        )
    lines += [
        (
            f"if {shlex.quote(curl)} --fail --silent --show-error --max-time 20 "
            "https://api.github.com >/dev/null; then echo 'api.github.com: allowed'; "
            "else echo 'api.github.com: FAILED'; failed=1; fi"
        ),
        'exit "$failed"',
    ]
    return "\n".join(lines)


def sandbox_command(
    value: Policy, executable: str, workspace: Path, script: str, directory: Path
) -> tuple[str, ...]:
    if value.harness is Harness.CODEX:
        generated = argv.harness(
            run_request(),
            value,
            worktree=str(workspace),
            brief_file=str(directory / "brief"),
        )
        # Use the CLI's local sandbox runner, with exactly the policy overrides.
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
            "-c",
            script,
        )
    assert isinstance(value.detail, ClaudePolicy)
    path = directory / "sandbox.json"
    path.write_text(json.dumps(value.detail.settings["sandbox"]))
    return (
        executable,
        "-y",
        "@anthropic-ai/sandbox-runtime@0.0.82",
        "--settings",
        str(path),
        "/bin/sh",
        "-c",
        script,
    )


def scratch_servers(
    stack: ExitStack, tmp_path: Path, tmux: str, errors: list[str]
) -> dict[str, str]:
    sockets: dict[str, str] = {}
    for location, parent in (
        ("TMPDIR", tempfile.gettempdir()),
        ("tmp", str(Path("/") / "tmp")),
        ("HOME", str(Path.home())),
    ):
        try:
            directory = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="ag-", dir=parent)
                )
            )
            directory.chmod(0o700)
            path = str(directory / "s")
            env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
            subprocess.run(
                [
                    tmux,
                    "-f",
                    "/dev/null",
                    "-S",
                    path,
                    "new-session",
                    "-d",
                    "/bin/cat",
                ],
                env=env,
                check=True,
                timeout=10,
            )
            stack.callback(
                subprocess.run,
                [tmux, "-S", path, "kill-server"],
                env=env,
                check=False,
                timeout=10,
                capture_output=True,
            )
            subprocess.run(
                [tmux, "-S", path, "list-sessions"],
                env=env,
                check=True,
                capture_output=True,
                timeout=10,
            )
            sockets[location] = path
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"socket setup {location}: {type(exc).__name__}")
    return sockets


@pytest.mark.skipif(
    os.environ.get("TBHBDEV_SANDBOX_LIVE") != "1",
    reason="set TBHBDEV_SANDBOX_LIVE=1 on a real host to test sandbox socket refusal",
)
@pytest.mark.parametrize("harness", Harness)
def test_build_sandboxes_refuse_user_sockets(tmp_path: Path, harness: Harness) -> None:
    installed = shutil.which(harness.value)
    if installed is None:
        pytest.skip(f"{harness}: harness binary not installed")
    host = LINUX if sys.platform == "linux" else MACOS
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
    tmux = shutil.which("tmux")
    curl = shutil.which("curl")
    assert tmux, "tmux is required for live probes"
    assert curl, "curl is required for live probes"
    errors: list[str] = []
    with ExitStack() as stack:
        sockets = scratch_servers(stack, tmp_path, tmux, errors)
        script = probe_script(sockets, tmux, curl)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        paths = RunPaths(
            str(Path.home()),
            str(workspace),
            str(tmp_path),
            str(tmp_path / "codex"),
            str(tmp_path / "audit"),
            uid=os.getuid(),
        )
        run = run_host.Run(
            runfiles.RunFiles(tmp_path / "runs", "probe"),
            run_request(),
            config,
            host,
            Path.home(),
            tmp_path,
            0,
            0,
        )
        (tmp_path / "codex").mkdir(mode=0o700)
        for location in ("TMPDIR", "tmp", "HOME"):
            if location not in sockets:
                report(f"{harness} {location}: socket setup denied")
        if harness is Harness.AGY:
            for profile in Profile:
                with pytest.raises(Invalid, match="agy has no OS sandbox"):
                    policy.build(
                        config,
                        host,
                        harness=harness,
                        profile=profile,
                        account="agent",
                        unsandboxed=False,
                        paths=paths,
                    )
                report(f"agy {profile}: refused by policy")
            return
        value = policy.build(
            config,
            host,
            harness=harness,
            profile=Profile.BUILD,
            account="agent",
            unsandboxed=False,
            paths=paths,
            denied=run_host.resolved_denies(run, paths),
        )
        executable = installed if harness is Harness.CODEX else shutil.which("npx")
        if executable is None:
            pytest.skip(f"{harness}: npx unavailable for pinned sandbox runtime")
        if harness is Harness.CLAUDE:
            report("claude: npx -y @anthropic-ai/sandbox-runtime@0.0.82")
        directory = tmp_path / harness.value
        directory.mkdir(mode=0o700, exist_ok=True)
        command = sandbox_command(value, executable, workspace, script, directory)
        result = subprocess.run(
            command,
            env=value.env | {"HOME": str(tmp_path), "CODEX_HOME": str(directory)},
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        for line in result.stdout.splitlines():
            report(f"{harness} {line}")
        if result.returncode != 0 or "api.github.com: allowed" not in result.stdout:
            errors.append(
                f"{harness}: sandbox probe failed ({result.returncode}): "
                f"{result.stderr[-1500:]}"
            )
            for location in sockets:
                if f"{location}: refused" not in result.stdout:
                    report(f"{harness} {location}: sandbox unavailable")
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_local_sandbox_commands_use_the_build_policy(
    tmp_path: Path, harness: Harness
) -> None:
    paths = RunPaths(
        str(tmp_path),
        str(tmp_path / "workspace"),
        str(tmp_path / "temp"),
        str(tmp_path / "codex"),
        str(tmp_path / "audit"),
    )
    value = policy.build(
        settings(),
        LINUX,
        paths=paths,
        account="agent",
        harness=harness,
        profile=Profile.BUILD,
        unsandboxed=False,
    )
    command = sandbox_command(value, "/example/sandbox", tmp_path, "probe", tmp_path)
    assert command[0] == "/example/sandbox"
    assert command[-3:] == ("/bin/sh", "-c", "probe")
    if harness is Harness.CODEX:
        assert command[1:4] == ("sandbox", "-P", "build")
        assert any(
            arg.startswith("features.network_proxy={enabled=true") for arg in command
        )
        assert any(arg.startswith("permissions.build=") for arg in command)
    else:
        assert isinstance(value.detail, ClaudePolicy)
        assert command[1:4] == (
            "-y",
            "@anthropic-ai/sandbox-runtime@0.0.82",
            "--settings",
        )
        assert (
            json.loads(Path(command[4]).read_text()) == value.detail.settings["sandbox"]
        )


def test_socket_probe_reports_each_result() -> None:
    script = probe_script(
        {"first": "/example/a", "second": "/example/b"},
        "/example/tmux",
        "/example/curl",
    )
    assert "/example/tmux -S /example/a list-sessions" in script
    assert "/example/tmux -S /example/b list-sessions" in script
    assert "first: refused" in script
    assert "second: refused" in script
    assert "api.github.com: allowed" in script
    assert "--max-time 20" in script
    assert script.endswith('exit "$failed"')


def test_scratch_servers_clean_up_only_their_own_sockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tempfile.TemporaryDirectory

    def directory(**_kwargs: object) -> tempfile.TemporaryDirectory[str]:
        return temporary(prefix="scratch-", dir=tmp_path)

    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", directory)
    monkeypatch.setattr(subprocess, "run", run)
    errors: list[str] = []
    with ExitStack() as stack:
        sockets = scratch_servers(stack, tmp_path, "/example/tmux", errors)
        assert set(sockets) == {"TMPDIR", "tmp", "HOME"}
        assert len(set(sockets.values())) == 3
        for path in sockets.values():
            assert Path(path).parent.stat().st_mode & 0o777 == 0o700
    assert not errors
    assert {command[2] for command in calls if command[-1] == "kill-server"} == set(
        sockets.values()
    )


def test_claude_live_probe_uses_available_npx_without_srt(tmp_path: Path) -> None:
    paths = RunPaths(
        str(tmp_path),
        str(tmp_path / "worktree"),
        str(tmp_path / "temp"),
        str(tmp_path / "codex"),
        str(tmp_path / "audit"),
    )
    value = policy.build(
        settings(),
        LINUX,
        paths=paths,
        account="agent",
        harness=Harness.CLAUDE,
        profile=Profile.BUILD,
        unsandboxed=False,
    )
    command = sandbox_command(value, "/example/npx", tmp_path, "probe", tmp_path)
    assert command[:3] == ("/example/npx", "-y", "@anthropic-ai/sandbox-runtime@0.0.82")


@pytest.mark.parametrize("harness", Harness)
def test_missing_harness_skips_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(
        pytest.skip.Exception, match=f"{harness}: harness binary not installed"
    ):
        test_build_sandboxes_refuse_user_sockets(tmp_path, harness)
