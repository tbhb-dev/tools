"""Tests for `tbhbdev --version` and the build identity it reports."""

from __future__ import annotations

import json
from importlib import metadata
from typing import TYPE_CHECKING

import pytest

from tbhbdev import cli, version

if TYPE_CHECKING:
    from conftest import Installed

COMMIT = "135933fb00d166e18826bd61de5d4a5a8626b22d"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            json.dumps(
                {
                    "url": "https://github.com/tbhb-dev/tools",
                    "vcs_info": {"vcs": "git", "commit_id": COMMIT},
                }
            ),
            COMMIT,
        ),
        (
            json.dumps({"url": "https://x/y", "vcs_info": {"vcs": "git"}}),
            "unknown",
        ),
        (
            json.dumps({"url": "https://x/y", "vcs_info": {"commit_id": ""}}),
            "unknown",
        ),
        (
            json.dumps({"url": "file:///src/tools", "dir_info": {"editable": True}}),
            "editable /src/tools",
        ),
        (
            json.dumps({"url": "file:///src/tools", "dir_info": {"editable": False}}),
            "local /src/tools",
        ),
        (json.dumps({"url": "file:///src/tools", "dir_info": {}}), "local /src/tools"),
        (
            json.dumps(
                {"url": "file:///src/my%20tools", "dir_info": {"editable": True}}
            ),
            "editable /src/my tools",
        ),
        (
            json.dumps({"url": "file:///src/tools", "dir_info": {"editable": "yes"}}),
            "local /src/tools",
        ),
        (
            json.dumps(
                {
                    "url": "file:///src/tools",
                    "dir_info": {"editable": True},
                    "vcs_info": {"commit_id": COMMIT},
                }
            ),
            COMMIT,
        ),
        (
            json.dumps(
                {
                    "url": "file:///src/tools",
                    "vcs_info": {"commit_id": 7},
                    "dir_info": {"editable": True},
                }
            ),
            "editable /src/tools",
        ),
        (None, "unknown"),
        ("", "unknown"),
        ("{not json", "unknown"),
        ("[]", "unknown"),
        ('"abc"', "unknown"),
        ("null", "unknown"),
        ("{}", "unknown"),
        (json.dumps({"vcs_info": "git"}), "unknown"),
        (json.dumps({"vcs_info": {"commit_id": ["a"]}}), "unknown"),
        (json.dumps({"dir_info": {"editable": True}}), "unknown"),
        (json.dumps({"url": 3, "dir_info": {"editable": True}}), "unknown"),
        (json.dumps({"url": "file:///a", "dir_info": "yes"}), "unknown"),
        (json.dumps({"url": "", "dir_info": {}}), "unknown"),
    ],
)
def test_identity(text: str | None, expected: str) -> None:
    assert version.identity(text) == expected


class FakeDist:
    def __init__(self, version: str, direct_url: str | None) -> None:
        self.version = version
        self.direct_url = direct_url
        self.read: list[str] = []

    def read_text(self, filename: str) -> str | None:
        self.read.append(filename)
        return self.direct_url


def test_line_reads_the_version_and_direct_url_of_the_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = FakeDist("0", json.dumps({"vcs_info": {"commit_id": COMMIT}}))
    names: list[str] = []

    def distribution(name: str) -> FakeDist:
        names.append(name)
        return dist

    monkeypatch.setattr(metadata, "distribution", distribution)
    assert version.line() == f"tbhbdev 0 ({COMMIT})"
    assert names == ["tbhbdev"]
    assert dist.read == ["direct_url.json"]


def test_line_without_direct_url_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:

    def distribution(_name: str) -> FakeDist:
        return FakeDist("1.2", None)

    monkeypatch.setattr(metadata, "distribution", distribution)
    assert version.line() == "tbhbdev 1.2 (unknown)"


def test_line_of_the_installed_package() -> None:
    dist = metadata.distribution("tbhbdev")
    build = version.identity(dist.read_text("direct_url.json"))
    assert version.line() == f"tbhbdev {metadata.version('tbhbdev')} ({build})"


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_flag_prints_one_line_to_stdout(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(version, "line", lambda: "tbhbdev 0 (abc)")
    assert cli.main([flag]) == 0
    captured = capsys.readouterr()
    assert captured.out == "tbhbdev 0 (abc)\n"
    assert captured.err == ""


def test_version_flag_works_with_the_real_distribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == version.line() + "\n"


def test_version_flag_ignores_a_broken_plugin(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed.add("broken", ImportError("no module named 'nowhere'"))
    monkeypatch.setattr(version, "line", lambda: "tbhbdev 0 (abc)")
    assert cli.main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "tbhbdev 0 (abc)\n"
    assert captured.err == ""


@pytest.mark.parametrize("flag", ["--versions", "-v", "--bogus", "-x"])
def test_an_unknown_option_still_fails(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([flag]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"tbhbdev: unknown tool {flag!r}\n{cli.usage(cli.TOOLS)}"


def test_usage_lists_the_version_flag() -> None:
    assert "tbhbdev --version\n" in cli.usage(cli.TOOLS)
