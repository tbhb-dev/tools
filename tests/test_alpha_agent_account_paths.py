"""A cached account home must never become a test's writable Codex home."""

import os
import sys
from pathlib import Path
lazy from collections.abc import Callable

import pytest

from tbhbdev.alpha.agent import credentials


@pytest.mark.parametrize("origin", ["Path.home", "expanduser", "HOME"])
@pytest.mark.parametrize(
    "operation",
    ["write", "bytes", "mkdir", "open", "os-open", "rename-out", "rename-in"],
)
def test_test_io_refuses_cached_account_codex_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    operation: str,
    guard_account_codex_writes: Callable[[Path], None],
) -> None:
    home = tmp_path / "account"
    monkeypatch.setenv("HOME", str(home))
    if origin == "Path.home":
        cached = Path.home()
    elif origin == "expanduser":
        cached = Path("~").expanduser()
    else:
        cached = Path(os.environ["HOME"])
    directory = cached / ".codex"
    directory.mkdir(parents=True)
    source = directory / "hooks.json"
    source.write_text("fixture")
    safe = tmp_path / "safe"
    safe.write_text("fixture")
    owner = sys.modules[guard_account_codex_writes.__module__]
    monkeypatch.setattr(owner, "ACCOUNT_CODEX_HOME", directory)
    monkeypatch.setenv("HOME", str(tmp_path / "scratch"))

    def attempt() -> None:
        if operation == "write":
            source.write_text("plant")
        elif operation == "bytes":
            source.write_bytes(b"plant")
        elif operation == "mkdir":
            (directory / "extra").mkdir()
        elif operation == "open":
            with source.open("w") as stream:
                stream.write("plant")
        elif operation == "os-open":
            descriptor = os.open(source, os.O_WRONLY | os.O_TRUNC)
            os.close(descriptor)
        elif operation == "rename-out":
            source.rename(tmp_path / "moved")
        else:
            safe.rename(source)

    with pytest.raises(
        AssertionError, match="tests must not write account Codex state"
    ):
        attempt()


def test_production_auth_copy_and_refresh_are_the_only_account_write_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_account_codex_writes: Callable[[Path], None],
) -> None:
    directory = tmp_path / "account" / ".codex"
    directory.mkdir(parents=True)
    source = directory / "auth.json"
    source.write_text(
        '{"tokens":{"id_token":"fixture-id","access_token":"fixture-access","refresh_token":"fixture-before"}}'
    )
    destination = tmp_path / "private/auth.json"
    destination.parent.mkdir()
    owner = sys.modules[guard_account_codex_writes.__module__]
    monkeypatch.setattr(owner, "ACCOUNT_CODEX_HOME", directory)
    before = credentials.auth_copy(source, destination)
    after = (
        '{"tokens":{"id_token":"fixture-id","access_token":"fixture-access",'
        '"refresh_token":"fixture-after"}}'
    )
    destination.write_text(after)
    assert credentials.auth_write_back(source, destination, before) is None
    assert source.read_text() == after
