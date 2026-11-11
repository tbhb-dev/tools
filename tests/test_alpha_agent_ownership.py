"""Host name claims coexist with the driver's existing ownership handoff."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.agent import ownership, run_host, runfiles
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "case", ["matching", "different", "missing", "symlink", "oversize", "encoding"]
)
@pytest.mark.usefixtures("agent_suffix_case")
def test_driver_owner_claim_preserves_existing_records(
    tmp_path: Path, case: str
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    owner = files.path("owner")
    if case == "symlink":
        target = tmp_path / "target"
        target.write_text("example-owner\n")
        owner.symlink_to(target)
    elif case == "encoding":
        owner.write_bytes(b"\xff")
    elif case != "missing":
        owner.write_text(
            "example-owner\n"
            if case == "matching"
            else "other-owner\n"
            if case == "different"
            else "x" * 1025
        )
    before = sorted(tmp_path.iterdir())
    if case == "matching":
        ownership.claim(files, owner="example-owner")
        assert owner.read_text() == "example-owner\n"
    else:
        with pytest.raises(ownership.RunExistsError) as caught:
            ownership.claim(files, owner="example-owner")
        assert str(caught.value) == "run 'example' has no matching owner"
        cause = caught.value.__cause__
        assert (cause is None) is (case == "different")
        if case == "oversize":
            assert isinstance(cause, Invalid)
            assert str(cause).startswith("run owner exceeds ")
    assert sorted(tmp_path.iterdir()) == before


@pytest.mark.usefixtures("agent_suffix_case")
def test_run_host_accepts_driver_owner_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    files.write("owner", "example-owner\n")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    def selected(_name: str) -> runfiles.RunFiles:
        return files

    monkeypatch.setattr(run_host, "_files", selected)

    def load(_args: object, _temporary: Path) -> tuple[run_host.Run, str]:
        message = "ownership accepted"
        raise Invalid(message)

    monkeypatch.setattr(run_host, "_load", load)
    assert run_host.main(["example", "--owner", "example-owner"], "run-host") == 1
    assert files.path("log").read_text() == "run refused: ownership accepted\n"
    assert files.path("owner").read_text() == "example-owner\n"


@pytest.mark.parametrize("size", [1024, 1025])
@pytest.mark.usefixtures("agent_suffix_case")
def test_driver_owner_record_size_boundary(tmp_path: Path, size: int) -> None:
    files = runfiles.RunFiles(tmp_path, "example")
    owner = "x" * (size - 1)
    files.write("owner", owner + "\n")
    if size == 1024:
        ownership.claim(files, owner=owner)
    else:
        with pytest.raises(ownership.RunExistsError) as caught:
            ownership.claim(files, owner=owner)
        assert str(caught.value) == "run 'example' has no matching owner"
        assert isinstance(caught.value.__cause__, Invalid)
    assert files.path("owner").read_text() == owner + "\n"
