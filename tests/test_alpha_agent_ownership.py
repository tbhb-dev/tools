"""The driver's owner file is accepted only when it is private and matches."""

from __future__ import annotations

import os
import subprocess
import sys
from functools import partial
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


def fifo(path: Path) -> None:
    os.mkfifo(path, 0o600)


def directory(path: Path) -> None:
    path.mkdir()


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o600)


def dangling(path: Path) -> None:
    path.symlink_to(path.parent / "missing")


def linked(path: Path) -> None:
    target = path.parent / "target"
    regular(target)
    path.symlink_to(target)


def absent(_path: Path) -> None:
    return None


def test_matching_private_owner_is_accepted(tmp_path: Path) -> None:
    files = run_files(tmp_path)
    regular(files.path("owner"))
    ownership.claim(files, expected="run-id")
    assert files.path("owner").read_text() == "run-id\n"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (absent, "no usable owner file"),
        (dangling, "no usable owner file"),
        (linked, "no usable owner file"),
        (directory, "different owner"),
        (private_directory, "different owner"),
        (fifo, "different owner"),
        (partial(regular, text="other\n"), "different owner"),
        (partial(regular, text="run-id"), "different owner"),
        (partial(regular, text="run-id\nmore\n"), "different owner"),
        (partial(regular, text="run-idx\n"), "different owner"),
        (partial(regular, mode=0o644), "different owner"),
        (partial(regular, mode=0o777), "different owner"),
        (partial(regular, mode=0o400), "different owner"),
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


def test_fifo_owner_is_refused_without_blocking(tmp_path: Path) -> None:
    files = run_files(tmp_path)
    fifo(files.path("owner"))
    # A blocking open of a FIFO with no writer never returns, so run the
    # check in a child with a bounded wait.
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from tbhbdev.alpha.agent import ownership, runfiles\n"
        "files = runfiles.RunFiles(Path(sys.argv[1]), 'example')\n"
        "try:\n"
        "    ownership.verify(files, 'run-id')\n"
        "except ownership.RunExistsError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(files.root)],
        timeout=30,
        check=False,
    )
    assert done.returncode == 0


def test_newline_only_owner_never_matches_an_empty_id(tmp_path: Path) -> None:
    files = run_files(tmp_path)
    regular(files.path("owner"), "\n")
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
