"""Host paths and script interpreters are resolved before execution."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agent_example import settings
from agent_host_example import bounded_file_reads

from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.model import Invalid, Os


def script(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("os", Os)
def test_host_path_is_fixed_and_scoped(os: Os, monkeypatch: pytest.MonkeyPatch) -> None:
    original = settings()
    configured = dataclasses.replace(
        original,
        sandbox=dataclasses.replace(original.sandbox, host_path=("/example/bin",)),
    )
    monkeypatch.setenv("PATH", "/untrusted")
    entries = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
    if os is Os.MACOS:
        entries += ("/opt/homebrew/bin", "/usr/local/bin")
    assert host_tools.search_path(configured, os) == (*entries, "/example/bin")
    before = host_tools.search()
    with host_tools.configured(configured, os):
        assert host_tools.search() == ":".join((*entries, "/example/bin"))
        assert Path(host_tools.resolve("git")).is_absolute()
    assert host_tools.search() == before


def test_resolution_follows_symlinks_and_refuses_missing(tmp_path: Path) -> None:
    target = script(tmp_path / "target", "#!/bin/sh\nexit 0\n")
    (tmp_path / "tool").symlink_to(target)
    assert host_tools.resolve("tool", str(tmp_path)) == target
    with pytest.raises(Invalid, match="host executable missing was not found"):
        host_tools.resolve("missing", str(tmp_path))


@pytest.mark.parametrize(
    "header", ["#!/bin/sh", "#!/usr/bin/env sh", "#!/usr/bin/env -S sh -e"]
)
def test_script_interpreter_paths(tmp_path: Path, header: str) -> None:
    executable = script(tmp_path / "tool", f"{header}\nexit '0'\n: second-line\n")
    found = host_tools.executable_paths(executable, "/usr/bin:/bin")
    expected = (executable, str(Path("/bin/sh").resolve()))
    if "env" in header:
        expected = (executable, str(Path("/usr/bin/env").resolve()), expected[-1])
    assert found == expected


@pytest.mark.parametrize(
    "header",
    [
        "#!sh",
        "#!",
        "#!/usr/bin/env",
        "#!/usr/bin/env -i sh",
        "#!/usr/bin/env NAME=value sh",
    ],
)
def test_invalid_script_interpreter(tmp_path: Path, header: str) -> None:
    executable = script(tmp_path / "tool", header + "\n")
    with pytest.raises(Invalid, match="host script"):
        host_tools.executable_paths(executable, "/usr/bin:/bin")


def test_interpreter_cycle(tmp_path: Path) -> None:
    path = tmp_path / "tool"
    executable = script(path, f"#!{path}\n")
    with pytest.raises(Invalid, match="interpreter cycle"):
        host_tools.executable_paths(executable, str(tmp_path))


@pytest.mark.parametrize(
    ("listing", "version", "supported"),
    [
        ("network_proxy experimental false\n", None, True),
        ("other stable true\n", None, False),
        (None, "codex-cli 0.164.0\n", True),
        (None, "codex-cli 0.166.0", True),
        (None, "codex-cli 0.163.9", False),
        (None, "unexpected", False),
    ],
)
def test_codex_feature_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listing: str | None,
    version: str | None,
    supported: bool,
) -> None:
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["/example/codex", "features", "list"]
        assert kwargs == {
            "env": {
                "HOME": str(tmp_path),
                "CODEX_HOME": str(tmp_path),
                "PATH": host_tools.search(),
            },
            "cwd": tmp_path,
            "text": True,
            "capture_output": True,
            "check": False,
            "timeout": 10,
        }
        calls.append("features")
        return subprocess.CompletedProcess(
            command,
            1 if listing is None else 0,
            listing or "",
            "error: unrecognized subcommand 'features'" if listing is None else "",
        )

    def output(command: list[str], **kwargs: object) -> str:
        assert command == ["/example/codex", "--version"]
        assert kwargs == {
            "env": {
                "HOME": str(tmp_path),
                "CODEX_HOME": str(tmp_path),
                "PATH": host_tools.search(),
            },
            "cwd": tmp_path,
            "text": True,
            "stderr": subprocess.DEVNULL,
            "timeout": 10,
        }
        calls.append("version")
        assert version is not None
        return version

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", output)
    assert host_tools.codex_proxy_supported("/example/codex", tmp_path) is supported
    assert calls == (["features", "version"] if listing is None else ["features"])


@pytest.mark.parametrize("error", [OSError(), subprocess.TimeoutExpired("codex", 10)])
def test_feature_check_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert host_tools.codex_proxy_supported("/example/codex", tmp_path) is False


def test_nonregular_executable_cannot_block_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    path = tmp_path / "pipe"
    os.mkfifo(path, 0o700)
    original = os.open

    def opened(name: str, flags: int) -> int:
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        return original(name, flags)

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(Invalid, match="not a regular file"):
        host_tools.executable_paths(str(path), str(tmp_path))


def test_installation_resolves_all_runtime_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    linked = tmp_path / "alias"
    linked.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(sys, "prefix", str(linked / "prefix"))
    monkeypatch.setattr(sys, "executable", str(linked / "python"))
    monkeypatch.setattr(sys, "argv", [str(linked / "tbhbdev")])
    assert host_tools.installation() == (str(root / "prefix"), str(root), str(root))


def test_executable_reads_are_bounded_and_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = script(tmp_path / "binary", "binary data")
    opened = os.open

    def open_checked(path: str, flags: int) -> int:
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        return opened(path, flags)

    monkeypatch.setattr(os, "open", open_checked)
    bounded_file_reads(monkeypatch, 4096)
    assert host_tools.executable_paths(executable, str(tmp_path)) == (executable,)


def test_env_interpreter_uses_the_policy_search_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = script(tmp_path / "tool", "#!/usr/bin/env selected\nexit 0\n")
    interpreter = script(tmp_path / "selected", "binary")
    monkeypatch.setenv("PATH", "/untrusted")
    assert host_tools.executable_paths(executable, str(tmp_path)) == (
        executable,
        str(Path("/usr/bin/env").resolve()),
        interpreter,
    )


def test_feature_listing_errors_cannot_fall_back_to_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "configuration error")

    def version(*_args: object, **_kwargs: object) -> str:
        pytest.fail("a failed feature check must refuse")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", version)
    assert host_tools.codex_proxy_supported("/example/codex", tmp_path) is False
