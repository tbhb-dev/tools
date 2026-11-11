"""Disposable host controls and commands for sandbox escape probes."""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import termios
import tty
import zlib
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

lazy from test_alpha_agent_sandbox_live import probe_run, sandbox_command

lazy from tbhbdev import userconfig
lazy from tbhbdev.alpha.agent import credentials, host_tools, run_host, worktree
lazy from tbhbdev.alpha.agent.model import Harness

if TYPE_CHECKING:
    import pytest

    from tbhbdev.alpha.agent.model import Policy, Profile, RunPaths


def live_policy(
    root: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, profile: Profile
) -> tuple[run_host.Run, RunPaths]:
    run, paths = probe_run(root, harness)
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "protected"))
    installed = shutil.which(harness.value)
    assert installed is not None
    search = (str(Path(installed).parent), str(Path(sys.executable).parent))
    config = dataclasses.replace(
        run.settings,
        sandbox=dataclasses.replace(
            run.settings.sandbox,
            host_path=search,
            path=dataclasses.replace(
                run.settings.sandbox.path,
                linux=(*search, "/usr/bin", "/bin"),
                macos=(*search, "/usr/bin", "/bin"),
            ),
        ),
    )
    return (
        dataclasses.replace(
            run,
            settings=config,
            request=dataclasses.replace(run.request, profile=profile),
        ),
        paths,
    )


def checked_live_policy(run: run_host.Run, paths: RunPaths) -> tuple[Policy, str]:
    with host_tools.configured(run.settings, run.host.os):
        return run_host.checked_policy(run, paths)


def codex_sandbox_command(
    run: run_host.Run,
    paths: RunPaths,
    value: Policy,
    executable: str,
    *arguments: str,
) -> tuple[str, ...]:
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )
    rendered = run_host.harness_command(run, prepared, value, executable)
    prefixes = ("default_permissions=", f"permissions.{value.profile.value}=")
    overrides = tuple(
        word
        for index, word in enumerate(rendered)
        if word.startswith(prefixes)
        or (word == "-c" and rendered[index + 1].startswith(prefixes))
    )
    return executable, "sandbox", *overrides, "--", *arguments


def protected_sources(run: run_host.Run) -> tuple[Path, ...]:
    readable = userconfig.path()
    readable.parent.mkdir(parents=True)
    readable.write_text('value = "original"\n')
    mirror = worktree.mirror_path(run.home, run.request.repo)
    objects = mirror / "objects"
    data = b"blob 9\0original\n"
    digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    object_file = objects / digest[:2] / digest[2:]
    object_file.parent.mkdir(parents=True)
    object_file.write_bytes(zlib.compress(data))
    config = mirror / "config"
    config.write_text("[core]\nbare = true\n")
    unlisted = run.tmpdir.parent / "unlisted"
    unlisted.write_text("original\n")
    sources = (readable, config, object_file, unlisted)
    for source in sources:
        source.chmod(0o600)
    return sources


def snapshot(path: Path) -> tuple[bytes, int, int, int, int]:
    info = path.stat()
    return path.read_bytes(), info.st_mode, info.st_ino, info.st_nlink, info.st_dev


def assert_unchanged(path: Path, expected: tuple[bytes, int, int, int, int]) -> None:
    assert snapshot(path) == expected, f"protected target changed: {path.name}"


def link_control(source: Path, writes: Path, controls: Path, ln: str) -> None:
    assert source.stat().st_dev == writes.stat().st_dev, "different filesystems"
    linked = writes / source.name
    subprocess.run([ln, str(source), str(linked)], check=True, timeout=10)
    assert linked.stat().st_ino == source.stat().st_ino
    linked.rename(controls / source.name)


def hard_link_attempt(source: Path, linked: Path, ln: str) -> dict[str, int]:
    result = subprocess.run(
        [ln, str(source), str(linked)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    codes = {"link": result.returncode}
    try:
        source.read_bytes()
        codes["read"] = 0
    except OSError as exc:
        codes["read"] = exc.errno or 1
    try:
        with linked.open("r+b") as stream:
            stream.write(b"changed")
            stream.truncate()
        codes["write"] = 0
    except OSError as exc:
        codes["write"] = exc.errno or 1
    try:
        linked.chmod(0o700)
        codes["chmod"] = 0
    except OSError as exc:
        codes["chmod"] = exc.errno or 1
    return codes


INPUT_MARKER = b"agent-input-probe"


@dataclasses.dataclass(frozen=True)
class TerminalObservation:
    inputs: bytes
    code: int


def observe_terminal(
    command: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
    *,
    prompt: str | None = None,
    tty_file: Path | None = None,
) -> TerminalObservation:
    # Codex's explicit '-' calls read_to_end even on a terminal. Raw-mode EOF
    # is data, so give the prompt as an argument while retaining terminal stdin.
    # resolve_root_prompt/read_prompt_from_stdin skip optional stdin on a tty:
    # https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/exec/src/lib.rs
    if prompt is not None:
        command = (*command[:-1], prompt)
    master, slave = pty.openpty()
    try:
        if tty_file is not None:
            tty_file.write_text(os.ttyname(slave))
        with subprocess.Popen(
            (sys.executable, str(Path(__file__).resolve()), "terminal", *command),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd=cwd,
            start_new_session=True,
        ) as child:
            try:
                output, _ = child.communicate(timeout=45)
                code = child.returncode
            except subprocess.TimeoutExpired:
                code = 124
                output = b""
            finally:
                # Only this process's freshly created session receives signals.
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        inputs = bytes.fromhex(json.loads(output)["inputs"]) if output else b""
        return TerminalObservation(inputs, code)
    finally:
        os.close(slave)
        os.close(master)


@dataclasses.dataclass(frozen=True)
class TerminalAttempt:
    attempted: bool
    error: int
    missing_linux_tty: bool = False


def terminal_status(
    control: bytes,
    observed: bytes,
    attempt: TerminalAttempt,
    *,
    kernel_blocked: bool = False,
) -> str:
    if attempt.error == 0 or INPUT_MARKER in observed:
        return "injected"
    if not attempt.attempted:
        # Codex rust-v0.164.0, linux-sandbox/src/bwrap.rs:504-515 mounts a
        # minimal /dev. The host's /dev/pts terminal is absent in that view.
        if (
            attempt.missing_linux_tty
            and attempt.error == errno.ENOENT
            and (INPUT_MARKER in control or kernel_blocked)
        ):
            return "blocked by device namespace"
        return "unproven"
    if kernel_blocked:
        return "blocked by kernel"
    if INPUT_MARKER not in control:
        return "unproven"
    return "blocked"


def inject_terminal(witness: Path, terminal: str = "/dev/tty") -> None:
    attempted = False
    error = 0
    try:
        descriptor = os.open(terminal, os.O_RDONLY | os.O_NOCTTY)
        try:
            for byte in INPUT_MARKER:
                attempted = True
                fcntl.ioctl(descriptor, termios.TIOCSTI, bytes([byte]))
        finally:
            os.close(descriptor)
    except OSError as exc:
        error = exc.errno or 1
    witness.write_text(json.dumps({"attempted": attempted, "error": error}))


def legacy_input_setting() -> str:
    setting = Path("/proc/sys/dev/tty/legacy_tiocsti")
    return setting.read_text().strip() if sys.platform == "linux" else "unavailable"


def probe_command(
    run: run_host.Run, paths: RunPaths, value: Policy, executable: str, script: Path
) -> tuple[str, ...]:
    command = list(sandbox_command(run, paths, value, executable, script))
    if value.harness is not Harness.CODEX:
        command.append(run.files.path("prompt").read_text())
    return tuple(command)


def main(mode: str, arguments: list[str]) -> None:
    if mode == "terminal":
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        tty.setraw(0)
        result = subprocess.run(
            arguments, check=False, timeout=40, stdout=subprocess.DEVNULL
        )
        # macOS revokes the controlling tty when its session leader exits.
        # Collect queued input here, while that leader still owns the terminal.
        inputs = os.read(0, 4096) if select.select([0], [], [], 0)[0] else b""
        sys.stdout.write(json.dumps({"inputs": inputs.hex()}))
        raise SystemExit(result.returncode)
    if mode == "inject":
        # Tool shells can have no controlling terminal. Use the exact outer
        # tty recorded by observe_terminal, without requiring write-open access.
        terminal = Path(arguments[0]).read_text() if len(arguments) > 1 else "/dev/tty"
        inject_terminal(Path(arguments[-1]), terminal)
    else:
        results, writes, ln, *sources = arguments
        observed = {
            Path(source).name: hard_link_attempt(
                Path(source), Path(writes) / Path(source).name, ln
            )
            for source in sources
        }
        Path(results).write_text(json.dumps(observed))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
