"""Tests for `tbhbdev --version` and the build identity it reports."""

from __future__ import annotations

import json
import sys
from importlib import metadata
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from tbhbdev import cli, version

if TYPE_CHECKING:
    from pathlib import Path

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
    build = version.identity(dist.read_text("direct_url.json"), version.build_module())
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


def stamp_module(commit: object) -> ModuleType:
    module = ModuleType("tbhbdev._build")
    module.COMMIT = commit  # type: ignore[attr-defined]
    return module


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (None, None),
        (ModuleType("empty"), None),
        (stamp_module(None), None),
        (stamp_module(7), None),
        (stamp_module(""), None),
        (stamp_module(COMMIT[:39]), None),
        (stamp_module(COMMIT + "0"), None),
        (stamp_module(COMMIT.upper()), None),
        (stamp_module("g" * 40), None),
        (stamp_module(f"x{COMMIT}"), None),
        (stamp_module(f"{COMMIT}\n"), None),
        (stamp_module(COMMIT), COMMIT),
    ],
)
def test_stamped(build: ModuleType | None, expected: str | None) -> None:
    assert version.stamped(build) == expected


def test_a_recorded_install_outranks_a_stamp() -> None:
    """A leftover stamp in a checkout must not mask what pip recorded."""
    other = "a" * 40
    vcs = json.dumps({"vcs_info": {"commit_id": other}})
    editable = json.dumps({"url": "file:///src/x", "dir_info": {"editable": True}})
    assert version.identity(vcs, stamp_module(COMMIT)) == other
    assert version.identity(editable, stamp_module(COMMIT)) == "editable /src/x"


def test_a_stamp_names_an_install_with_no_vcs_or_directory() -> None:
    archive = json.dumps({"url": "https://x/y.whl", "archive_info": {}})
    assert version.identity(None, stamp_module(COMMIT)) == COMMIT
    assert version.identity(archive, stamp_module(COMMIT)) == COMMIT


def test_a_missing_or_bad_stamp_falls_back_to_direct_url() -> None:
    text = json.dumps({"vcs_info": {"commit_id": "b" * 40}})
    assert version.identity(text, None) == "b" * 40
    assert version.identity(text, stamp_module("nope")) == "b" * 40
    assert version.identity(None, stamp_module("nope")) == "unknown"


def test_build_module_imports_the_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    module = stamp_module(COMMIT)
    monkeypatch.setitem(sys.modules, "tbhbdev._build", module)
    assert version.build_module() is module


def test_build_module_is_none_without_the_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "tbhbdev._build", None)
    assert version.build_module() is None


def test_line_reports_direct_url_over_a_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "c" * 40
    dist = FakeDist("0", json.dumps({"vcs_info": {"commit_id": other}}))

    def distribution(_name: str) -> FakeDist:
        return dist

    monkeypatch.setattr(metadata, "distribution", distribution)
    monkeypatch.setattr(version, "build_module", lambda: stamp_module(COMMIT))
    assert version.line() == f"tbhbdev 0 ({other})"


def test_line_falls_back_to_the_stamp_without_direct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def distribution(_name: str) -> FakeDist:
        return FakeDist("0", None)

    monkeypatch.setattr(metadata, "distribution", distribution)
    monkeypatch.setattr(version, "build_module", lambda: stamp_module(COMMIT))
    assert version.line() == f"tbhbdev 0 ({COMMIT})"


@pytest.mark.parametrize("source", ["COMMIT = (\n", 'raise RuntimeError("x")\n'])
def test_build_module_skips_a_stamp_that_cannot_import(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "_build.py").write_text(source)
    monkeypatch.setattr(version, "BUILD_MODULE", "_build")
    monkeypatch.setattr(sys, "path", [str(package), *sys.path])
    monkeypatch.delitem(sys.modules, "_build", raising=False)
    assert version.build_module() is None


def test_line_leaves_the_stamp_alone_when_direct_url_names_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "c" * 40
    dist = FakeDist("0", json.dumps({"vcs_info": {"commit_id": other}}))

    def broken() -> ModuleType:
        msg = "the stamp was imported"
        raise AssertionError(msg)

    def distribution(_name: str) -> FakeDist:
        return dist

    monkeypatch.setattr(metadata, "distribution", distribution)
    monkeypatch.setattr(version, "build_module", broken)
    assert version.line() == f"tbhbdev 0 ({other})"
