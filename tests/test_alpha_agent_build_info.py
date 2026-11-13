"""Provenance is diagnostic and remains usable without install metadata or git."""

import json
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agent_host_example import build_provenance, constant

from tbhbdev.alpha.agent import build_info

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("commit", ["a" * 40, "b" * 64, "", "x" * 40, "a" * 39])
def test_wheel_commit(commit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        build_info.metadata,
        "distribution",
        constant(
            SimpleNamespace(
                read_text=constant(json.dumps({"vcs_info": {"commit_id": commit}}))
            )
        ),
    )
    assert build_info.wheel_commit() == (
        commit if commit in ("a" * 40, "b" * 64) else "unknown"
    )


@pytest.mark.parametrize(
    "error", [OSError, ValueError, build_info.metadata.PackageNotFoundError]
)
def test_wheel_metadata_unavailable(
    error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_name: str) -> None:
        raise error

    monkeypatch.setattr(build_info.metadata, "distribution", missing)
    assert build_info.wheel_commit() == "unknown"


@pytest.mark.parametrize("error", [OSError, subprocess.SubprocessError])
def test_git_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    build_provenance(tmp_path, monkeypatch, present=True)

    def missing(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "check_output", missing)
    assert build_info.tools_commit() == "unknown"
