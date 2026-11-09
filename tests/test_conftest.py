"""Tests for the fakes in conftest.py."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import pytest
    from conftest import Stubs


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
