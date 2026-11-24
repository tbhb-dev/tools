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


@pytest.mark.parametrize("prefix", ["", "tools/"])
@pytest.mark.parametrize("rc", [0, 1])
def test_build_succeeds_with_only_referenced_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int, prefix: str
) -> None:
    (tmp_path / "prek.toml").write_text(f"{prefix}.bazel_env/bin/gitleaks")
    package = tmp_path / prefix
    package.mkdir(exist_ok=True)
    (package / "BUILD.bazel").write_text(
        'bazel_env(name = "bazel_env", tools = [":gitleaks"])'
    )
    target = "//tools:bazel_env" if prefix else "//:bazel_env"
    command = ["/tools/bazelisk", "run", target]
    assert shims.needed(tmp_path, Profile.BUILD)

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        shim = package / ".bazel_env/bin/gitleaks"
        shim.parent.mkdir(parents=True)
        shim.touch()
        return subprocess.CompletedProcess(command, rc, b"", b"direnv is unavailable")

    fake = Mock(side_effect=run)
    monkeypatch.setattr(shims.subprocess, "run", fake)
    shims.build(tmp_path, command[0])
    assert not (package / ".bazel_env/bin/cog").exists()
    assert not shims.needed(tmp_path, Profile.BUILD)
    fake.assert_called_once_with(
        command, cwd=tmp_path, capture_output=True, timeout=600, check=False
    )


@pytest.mark.parametrize("rc", [0, 2])
def test_build_requires_referenced_shim_and_sanitizes_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    (tmp_path / "prek.toml").write_text(".bazel_env/bin/gitleaks")
    result = subprocess.CompletedProcess(
        [], rc, b"ignored", b"failed \xff in /private/path\nsecond line"
    )
    monkeypatch.setattr(shims.subprocess, "run", Mock(return_value=result))
    expected = (
        f"hook shims: bazel run //:bazel_env failed (rc={rc}); "
        "missing .bazel_env/bin/gitleaks: failed \ufffd"
    )
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
    (tmp_path / "prek.toml").write_text(".bazel_env/bin/gitleaks")
    monkeypatch.setattr(shims.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(Invalid, match=message):
        shims.build(tmp_path, "/tools/bazelisk")


def test_build_requires_bazel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Mock()
    monkeypatch.setattr(shims.subprocess, "run", fake)
    with pytest.raises(Invalid, match=r"^hook shims: bazelisk or bazel is required$"):
        shims.build(tmp_path, None)
    fake.assert_not_called()


@pytest.mark.parametrize("exists", [False, True])
def test_bare_directory_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    (tmp_path / "prek.toml").write_text("tools/.bazel_env/bin/")
    assert shims.needed(tmp_path, Profile.BUILD)
    fake = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b"detail"))
    monkeypatch.setattr(shims.subprocess, "run", fake)
    if exists:
        (tmp_path / "tools/.bazel_env/bin").mkdir(parents=True)
        shims.build(tmp_path, "bazel")
        assert not shims.needed(tmp_path, Profile.BUILD)
    else:
        with pytest.raises(Invalid, match=r"missing tools/\.bazel_env/bin: detail"):
            shims.build(tmp_path, "bazel")
    assert fake.call_args.args[0] == ["bazel", "run", "//tools:bazel_env"]


def test_all_configs_and_shims_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "prek.toml").write_text(
        ".bazel_env/bin/gitleaks .bazel_env/bin/gitleaks .bazel_env/bin/ruff"
    )
    (tmp_path / ".pre-commit-config.yaml").write_text(".bazel_env/bin/buildifier")
    directory = tmp_path / ".bazel_env/bin"
    directory.mkdir(parents=True)
    (directory / "gitleaks").touch()
    # Existing directories still skip preparation, even with missing shims.
    assert not shims.needed(tmp_path, Profile.BUILD)
    monkeypatch.setattr(
        shims.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, b"", b"detail")),
    )
    with pytest.raises(
        Invalid,
        match=r"missing \.bazel_env/bin/buildifier, \.bazel_env/bin/ruff: detail",
    ):
        shims.build(tmp_path, "bazel")


def test_multiple_packages_and_no_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr(shims.subprocess, "run", fake)
    shims.build(tmp_path, "bazel")
    fake.assert_not_called()
    (tmp_path / "prek.toml").write_text(
        ".bazel_env/bin/first tools/.bazel_env/bin/second"
    )
    for name in [".bazel_env/bin/first", "tools/.bazel_env/bin/second"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.touch()
    shims.build(tmp_path, "bazel")
    assert [call.args[0] for call in fake.call_args_list] == [
        ["bazel", "run", "//:bazel_env"],
        ["bazel", "run", "//tools:bazel_env"],
    ]
