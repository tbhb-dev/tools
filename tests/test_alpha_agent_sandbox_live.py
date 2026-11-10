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
    from tbhbdev.alpha.agent.model import Host, Policy, Settings


def report(line: str) -> None:
    sys.stdout.write(line + "\n")


def real_node() -> Path:
    candidates: list[str] = []
    mise = shutil.which("mise")
    if mise:
        try:
            result = subprocess.run(
                [mise, "which", "node"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            candidates.append(result.stdout.strip())
        except OSError, subprocess.SubprocessError:
            pass
    candidates.extend(str(Path(entry) / "node") for entry in os.get_exec_path())
    for candidate in candidates:
        path = Path(candidate).absolute()
        resolved = path.resolve()
        if (
            "shims" not in path.parts
            and "shims" not in resolved.parts
            and resolved.is_file()
            and os.access(resolved, os.X_OK)
        ):
            return resolved
    pytest.skip("claude: no real node executable outside shim directories")


def claude_runtime(directory: Path) -> tuple[str, str]:
    node = real_node()
    npm = (node.parent / "npm").resolve()
    # mise can replace bin/npm with a shell wrapper rather than a symlink.
    if npm.suffix != ".js":
        npm = node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"
    if not npm.is_file():
        pytest.skip("claude: npm CLI unavailable beside real node")
    # Install only into the disposable probe directory. Launch npm's JavaScript
    # and the pinned runtime CLI with real node, bypassing both shebangs.
    try:
        subprocess.run(
            [
                str(node),
                str(npm),
                "install",
                "--prefix",
                str(directory),
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "@anthropic-ai/sandbox-runtime@0.0.82",
            ],
            env={
                "HOME": str(directory),
                "PATH": str(node.parent) + os.pathsep + os.defpath,
            },
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(
            f"claude: pinned sandbox runtime unavailable ({type(exc).__name__})"
        )
    cli = directory / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    if not cli.is_file():
        pytest.skip("claude: pinned sandbox runtime CLI unavailable")
    return str(node), str(cli.resolve())


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
    value: Policy,
    executable: tuple[str, ...],
    workspace: Path,
    script: str,
    directory: Path,
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
            *executable,
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
        *executable,
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
            report_agy_refusals(config, host, paths)
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
        directory = tmp_path / harness.value
        directory.mkdir(mode=0o700, exist_ok=True)
        executable = (installed,)
        if harness is Harness.CLAUDE:
            executable = claude_runtime(directory)
            report("claude: real node @anthropic-ai/sandbox-runtime@0.0.82")
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
    executable = (
        ("/example/sandbox",)
        if harness is Harness.CODEX
        else ("/example/node", "/example/cli.js")
    )
    command = sandbox_command(value, executable, tmp_path, "probe", tmp_path)
    assert command[: len(executable)] == executable
    assert command[-3:] == ("/bin/sh", "-c", "probe")
    if harness is Harness.CODEX:
        assert command[1:4] == ("sandbox", "-P", "build")
        assert any(
            arg.startswith("features.network_proxy={enabled=true") for arg in command
        )
        assert any(arg.startswith("permissions.build=") for arg in command)
    else:
        assert isinstance(value.detail, ClaudePolicy)
        assert command[2] == "--settings"
        assert (
            json.loads(Path(command[3]).read_text()) == value.detail.settings["sandbox"]
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


@pytest.mark.parametrize(
    "mise_result", ["managed/node", "shims/node", "missing", "error", None]
)
def test_real_node_bypasses_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mise_result: str | None
) -> None:
    node = tmp_path / "node"
    node.write_text("real node")
    node.chmod(0o700)
    managed = tmp_path / "managed/node"
    managed.parent.mkdir()
    managed.write_text("managed node")
    managed.chmod(0o700)
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "node").symlink_to(node)

    def which(_name: str) -> str | None:
        return "/example/mise" if mise_result else None

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(os, "get_exec_path", lambda: [str(shims), str(tmp_path)])

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["/example/mise", "which", "node"]
        if mise_result == "error":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, str(tmp_path / str(mise_result)))

    monkeypatch.setattr(subprocess, "run", run)
    assert real_node() == (managed if mise_result == "managed/node" else node)


def test_missing_real_node_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def which(_name: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(os, "get_exec_path", lambda: [str(tmp_path)])
    with pytest.raises(pytest.skip.Exception, match="no real node executable"):
        real_node()


@pytest.mark.parametrize("failure", [None, "npm", "install", "cli", "wrapper"])
def test_claude_runtime_uses_real_node_and_disposable_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    node = tmp_path / "bin/node"
    node.parent.mkdir()
    node.touch()
    monkeypatch.setattr(sys.modules[__name__], "real_node", lambda: node)
    npm = tmp_path / "lib/node_modules/npm/bin/npm-cli.js"
    if failure != "npm":
        npm.parent.mkdir(parents=True)
        npm.touch()
    if failure == "wrapper":
        (node.parent / "npm").write_text("#!/bin/sh\nexit 1\n")
    else:
        (node.parent / "npm").symlink_to(npm)
    cli = tmp_path / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == [
            str(node),
            str(npm),
            "install",
            "--prefix",
            str(tmp_path),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "@anthropic-ai/sandbox-runtime@0.0.82",
        ]
        assert kwargs["env"] == {
            "HOME": str(tmp_path),
            "PATH": str(node.parent) + os.pathsep + os.defpath,
        }
        assert kwargs["cwd"] == tmp_path
        if failure == "install":
            raise subprocess.CalledProcessError(1, command)
        if failure != "cli":
            cli.parent.mkdir(parents=True)
            cli.touch()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    if failure in {"npm", "install", "cli"}:
        with pytest.raises(pytest.skip.Exception, match=r"claude: .*unavailable"):
            claude_runtime(tmp_path)
    else:
        assert claude_runtime(tmp_path) == (str(node), str(cli))


@pytest.mark.parametrize("harness", Harness)
def test_missing_harness_skips_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def which(_name: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", which)
    with pytest.raises(
        pytest.skip.Exception, match=f"{harness}: harness binary not installed"
    ):
        test_build_sandboxes_refuse_user_sockets(tmp_path, harness)
