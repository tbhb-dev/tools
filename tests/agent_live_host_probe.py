"""Disposable host controls and commands for sandbox escape probes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

from test_alpha_agent_sandbox_live import probe_run

from tbhbdev.alpha.agent import host_tools, run_host, worktree

if TYPE_CHECKING:
    import pytest

    from tbhbdev.alpha.agent.model import Harness, Policy, Profile, RunPaths


def live_policy(
    root: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness, profile: Profile
) -> tuple[run_host.Run, RunPaths]:
    run, paths = probe_run(root, harness)
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
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
            home=home,
            settings=config,
            request=dataclasses.replace(run.request, profile=profile),
        ),
        dataclasses.replace(paths, home=str(home)),
    )


def checked_live_policy(run: run_host.Run, paths: RunPaths) -> tuple[Policy, str]:
    with host_tools.configured(run.settings, run.host.os):
        return run_host.checked_policy(run, paths)


def protected_sources(run: run_host.Run) -> tuple[Path, ...]:
    readable = run.home / ".gitconfig"
    readable.write_text("[example]\nvalue = original\n")
    mirror = worktree.mirror_path(run.home, run.request.repo)
    objects = mirror / "objects"
    data = b"blob 9\0original\n"
    digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    object_file = objects / digest[:2] / digest[2:]
    object_file.parent.mkdir(parents=True)
    object_file.write_bytes(zlib.compress(data))
    config = mirror / "config"
    config.write_text("[core]\nbare = true\n")
    sources = (readable, config, object_file)
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


def hardlink_attempt(source: Path, linked: Path, ln: str) -> dict[str, int]:
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


def main(arguments: list[str]) -> None:
    results, writes, ln, *sources = arguments
    observed = {
        Path(source).name: hardlink_attempt(
            Path(source), Path(writes) / Path(source).name, ln
        )
        for source in sources
    }
    Path(results).write_text(json.dumps(observed))


if __name__ == "__main__":
    main(sys.argv[2:])
