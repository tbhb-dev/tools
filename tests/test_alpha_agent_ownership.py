"""The driver's owner file is accepted only when it is private and matches."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.agent import ownership, runfiles

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def run_files(tmp_path: Path) -> runfiles.RunFiles:
    return runfiles.RunFiles(tmp_path / "runs", "example")


def regular(path: Path, text: str = "run-id\n", mode: int = 0o600) -> None:
    path.write_text(text)
    path.chmod(mode)


def directory(path: Path) -> None:
    path.mkdir()


def dangling(path: Path) -> None:
    path.symlink_to(path.parent / "missing")


def linked(path: Path) -> None:
    target = path.parent / "target"
    regular(target)
    path.symlink_to(target)


def test_matching_private_owner_is_accepted(tmp_path: Path) -> None:
    files = run_files(tmp_path)
    regular(files.path("owner"))
    ownership.claim(files, expected="run-id")
    assert files.path("owner").read_text() == "run-id\n"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda _path: None, "no usable owner file"),
        (dangling, "no usable owner file"),
        (linked, "no usable owner file"),
        (directory, "different owner"),
        (lambda path: regular(path, "other\n"), "different owner"),
        (lambda path: regular(path, "run-id"), "different owner"),
        (lambda path: regular(path, "run-id\nmore\n"), "different owner"),
        (lambda path: regular(path, "run-idx\n"), "different owner"),
        (lambda path: regular(path, mode=0o644), "different owner"),
        (lambda path: regular(path, mode=0o777), "different owner"),
        (lambda path: regular(path, mode=0o400), "different owner"),
    ],
)
def test_unusable_owner_is_refused(
    tmp_path: Path, make: Callable[[Path], None], message: str
) -> None:
    files = run_files(tmp_path)
    make(files.path("owner"))
    with pytest.raises(ownership.RunExistsError, match=message):
        ownership.claim(files, expected="run-id")


def test_owner_of_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = run_files(tmp_path)
    regular(files.path("owner"))
    other = os.getuid() + 1
    monkeypatch.setattr(ownership.os, "getuid", lambda: other)
    with pytest.raises(ownership.RunExistsError, match="different owner"):
        ownership.claim(files, expected="run-id")


def test_empty_expected_id_is_still_verified(tmp_path: Path) -> None:
    files = run_files(tmp_path)
    regular(files.path("owner"))
    with pytest.raises(ownership.RunExistsError, match="different owner"):
        ownership.claim(files, expected="")


def test_verification_never_creates_the_owner(tmp_path: Path) -> None:
    files = run_files(tmp_path)
    with pytest.raises(ownership.RunExistsError):
        ownership.claim(files, expected="run-id")
    assert not files.path("owner").exists()


def test_direct_claim_creates_a_private_owner(tmp_path: Path) -> None:
    files = run_files(tmp_path)
    ownership.claim(files)
    assert files.path("owner").read_text() == "host\n"
    assert files.path("owner").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ownership.RunExistsError, match="already has an owner"):
        ownership.claim(files)
