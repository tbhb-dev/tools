"""Tests for the fakes in conftest.py."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev import cue_runtime

if TYPE_CHECKING:
    from conftest import Stubs


@pytest.mark.parametrize("bazel", [False, True])
def test_pinned_cue_uses_bazel_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    bazel: bool,
) -> None:
    local_data = tmp_path / "local"
    monkeypatch.setenv("XDG_DATA_HOME", str(local_data))
    binary = tmp_path / "cue"
    binary.symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tmp_path))
    if bazel:
        monkeypatch.setenv("TEST_TMPDIR", str(tmp_path))
    else:
        monkeypatch.delenv("TEST_TMPDIR", raising=False)

    request.getfixturevalue("pinned_cue")

    target = cue_runtime.binary_path()
    if bazel:
        assert target.is_relative_to(tmp_path)
        assert target.readlink() == binary
    else:
        assert target.is_relative_to(local_data)
        assert not target.exists()


def test_stub_runs_under_long_interpreter_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A `#!` line over 256 bytes fails on Linux; the /bin/sh launcher doesn't."""
    deep = tmp_path / ("it's a " + "d" * 200) / ("e" * 200)
    deep.mkdir(parents=True)
    python = deep / "python"
    python.symlink_to(sys.executable)
    assert len(f"#!{python} -I") > 256
    monkeypatch.setattr(sys, "executable", str(python))
    stubs = cast("Stubs", request.getfixturevalue("stubs"))

    run = subprocess.run(
        [str(stubs.bin / "id"), "-un"], capture_output=True, text=True, check=False
    )
    assert (run.returncode, run.stdout, run.stderr) == (0, "operator\n", "")

    odd = ["", "a b", "'", '"$HOME"', "-un"]
    subprocess.run([str(stubs.bin / "id"), *odd], capture_output=True, check=False)
    assert stubs.argvs("id") == [["-un"], odd]


def test_operator_credentials_are_isolated(tmp_path: Path) -> None:
    op = shutil.which("op")
    assert op is not None
    assert Path(op).parent.name.startswith("no-credentials")
    assert not Path(op).is_relative_to(tmp_path)
    run = subprocess.run(["op", "whoami"], capture_output=True, text=True, check=False)
    assert (run.returncode, run.stdout, run.stderr) == (
        1,
        "",
        "tests: real 1Password access is disabled\n",
    )
    sock = Path(os.environ["SSH_AUTH_SOCK"])
    assert sock == tmp_path / "no-ssh-agent"
    assert not sock.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["xdg"]  # another autouse fixture
