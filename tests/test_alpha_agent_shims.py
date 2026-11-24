"""Hook shim detection and Bazel outcomes without running Bazel."""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tbhbdev.alpha.agent import shims
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "name", [".pre-commit-config.yaml", ".pre-commit-config.yml", "prek.toml"]
)
def test_needed_with_hook_reference(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text(".bazel_env/bin/cog")
    assert shims.needed(tmp_path, Profile.BUILD)
    assert not shims.needed(tmp_path, Profile.REVIEW)
    (tmp_path / ".bazel_env/bin").mkdir(parents=True)
    assert not shims.needed(tmp_path, Profile.BUILD)


def test_unneeded_with_no_hook_reference(tmp_path: Path) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text("entry: cog")
    assert not shims.needed(tmp_path, Profile.BUILD)


@pytest.mark.parametrize("rc", [0, 1])
def test_build_succeeds_when_cog_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    command = ["/tools/bazelisk", "run", "//:bazel_env"]

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        cog = tmp_path / ".bazel_env/bin/cog"
        cog.parent.mkdir(parents=True)
        cog.touch()
        return subprocess.CompletedProcess(command, rc, b"", b"direnv is unavailable")

    fake = Mock(side_effect=run)
    monkeypatch.setattr(shims.subprocess, "run", fake)
    shims.build(tmp_path, command[0])
    fake.assert_called_once_with(
        command, cwd=tmp_path, capture_output=True, timeout=600, check=False
    )


@pytest.mark.parametrize("rc", [0, 2])
def test_build_requires_cog_and_sanitizes_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    result = subprocess.CompletedProcess(
        [], rc, b"ignored", b"failed \xff in /private/path\nsecond line"
    )
    monkeypatch.setattr(shims.subprocess, "run", Mock(return_value=result))
    expected = f"hook shims: bazel run //:bazel_env failed (rc={rc}): failed \ufffd"
    with pytest.raises(Invalid, match=re.escape(expected)) as caught:
        shims.build(tmp_path, "/tools/bazel")
    assert "/private/path" not in str(caught.value)
    assert "\n" not in str(caught.value)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired("bazelisk", 600), "timed out after 600 seconds"),
        (FileNotFoundError(), "could not start"),
    ],
)
def test_build_refuses_subprocess_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    monkeypatch.setattr(shims.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(Invalid, match=message):
        shims.build(tmp_path, "/tools/bazelisk")


def test_build_requires_bazel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Mock()
    monkeypatch.setattr(shims.subprocess, "run", fake)
    with pytest.raises(Invalid, match=r"^hook shims: bazelisk or bazel is required$"):
        shims.build(tmp_path, None)
    fake.assert_not_called()
