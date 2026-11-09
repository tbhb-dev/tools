# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha session-context, against a config in a temporary dir."""

import platform
import time
from datetime import UTC, datetime
lazy from collections.abc import Iterator
lazy from pathlib import Path

import pytest

from tbhbdev.alpha import session_context

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


class Clock(datetime):
    @classmethod
    def now(cls, _tz: object = None) -> datetime:  # type: ignore[override]
        return NOW


@pytest.fixture
def _fixed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setattr("socket.gethostname", lambda: "example-a.local")
    monkeypatch.setattr("getpass.getuser", lambda: "example-user")
    monkeypatch.setattr(session_context, "os_name", lambda: "ExampleOS 1")
    monkeypatch.setattr(session_context, "datetime", Clock)
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("_fixed")
def test_it_names_the_host_its_role_the_others_and_the_time(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text(
        '[harness]\nhosts = ["example-a=peer Mac", '
        '"example-b = Linux VM", "example-c"]\n'
    )
    assert session_context.main() == 0
    assert capsys.readouterr().out == (
        "Session host: this session runs on example-a (peer Mac), ExampleOS 1, "
        "as user example-user.\n"
        "Other hosts: example-b, example-c. Reach them only over ssh; "
        '"this machine" and "local" mean example-a.\n'
        "Local time at session start: 2026-01-02 03:04 UTC.\n"
    )


@pytest.mark.usefixtures("_fixed")
def test_an_unlisted_host_has_no_role_and_a_lone_host_has_no_others(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text('[harness]\nhosts = ["example-z=Linux VM"]\n')
    session_context.main()
    unlisted = capsys.readouterr().out
    assert unlisted.startswith("Session host: this session runs on example-a, ")
    assert "Other hosts: example-z." in unlisted
    config_file.write_text('[harness]\nhosts = ["example-a"]\n')
    session_context.main()
    lone = capsys.readouterr().out
    assert lone.startswith("Session host: this session runs on example-a, ")
    assert "Other hosts" not in lone


@pytest.mark.parametrize(
    "text", [None, "[other]\nkey = 1\n", "[harness]\nrule = 'x'\n"]
)
@pytest.mark.usefixtures("_fixed")
def test_without_hosts_it_prints_nothing(
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
    text: str | None,
) -> None:
    if text is not None:
        config_file.write_text(text)
    assert session_context.main() == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


@pytest.mark.usefixtures("_fixed")
def test_an_invalid_config_prints_to_stderr_and_exits_0(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text("[harness]\nhosts = []\n")
    assert session_context.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("session-context: ")
    assert "[harness] hosts must be a non-empty list of strings" in captured.err


def test_the_real_clock_and_host_work(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text('[harness]\nhosts = ["example-q"]\n')
    assert session_context.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Session host: this session runs on ")
    assert lines[1].startswith("Other hosts: example-q.")
    assert lines[2].startswith("Local time at session start: 20")


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Darwin", "macOS 15.1"),
        ("Linux", "Linux Example Linux 1"),
        ("Plan9", "Plan9 9.1"),
    ],
)
def test_os_name(monkeypatch: pytest.MonkeyPatch, system: str, expected: str) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "mac_ver", lambda: ("15.1", ("", "", ""), ""))
    monkeypatch.setattr(
        platform, "freedesktop_os_release", lambda: {"PRETTY_NAME": "Example Linux 1"}
    )
    monkeypatch.setattr(platform, "release", lambda: "9.1")
    assert session_context.os_name() == expected


def test_os_name_on_linux_without_an_os_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> dict[str, str]:
        raise OSError

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "freedesktop_os_release", missing)
    assert session_context.os_name() == "Linux"
    monkeypatch.setattr(platform, "freedesktop_os_release", dict)
    assert session_context.os_name() == "Linux "


@pytest.mark.usefixtures("_fixed")
def test_context_runs_outside_a_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_file.write_text('[harness]\nhosts = ["example-a"]\n')
    monkeypatch.chdir(tmp_path)
    assert session_context.main() == 0
    assert capsys.readouterr().out.startswith(
        "Session host: this session runs on example-a, "
    )
