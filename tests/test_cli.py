"""Tests for the `tbhbdev` command's dispatch to its tools."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev import cli

if TYPE_CHECKING:
    from conftest import Installed

USAGE = cli.usage(cli.TOOLS)


def test_no_arguments_prints_usage_to_stderr_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == USAGE


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_to_stdout(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([flag]) == 0
    captured = capsys.readouterr()
    assert captured.out == USAGE
    assert captured.err == ""


def test_usage_lists_every_tool() -> None:
    for name in cli.TOOLS:
        assert f"  {name}\n" in USAGE


def test_unknown_tool_fails_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["nope", "x"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"tbhbdev: unknown tool 'nope'\n{USAGE}"


def test_tool_gets_the_rest_of_argv_and_its_program_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str]] = []

    def tool(argv: list[str], prog: str) -> int:
        calls.append((argv, prog))
        return 7

    monkeypatch.setitem(cli.TOOLS, "fake", tool)
    assert cli.main(["fake", "a", "--b"]) == 7
    assert calls == [(["a", "--b"], "tbhbdev fake")]


def test_main_reads_sys_argv_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["tbhbdev", "--help"])
    assert cli.main() == 0


def test_module_entry_point_runs_the_command() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == USAGE


def plugin_tool(argv: list[str], prog: str) -> int:
    sys.stdout.write(f"{prog} {' '.join(argv)}\n")
    return 5


def test_a_plugin_tool_is_listed_and_runs(
    installed: Installed, capsys: pytest.CaptureFixture[str]
) -> None:
    installed.stub("extra", tools={"zz-extra": plugin_tool})
    assert cli.main(["--help"]) == 0
    assert capsys.readouterr().out == cli.usage({**cli.TOOLS, "zz-extra": plugin_tool})
    assert cli.main(["zz-extra", "a"]) == 5
    assert capsys.readouterr().out == "tbhbdev zz-extra a\n"


def test_a_plugin_tool_that_shadows_a_built_in_fails(
    installed: Installed, capsys: pytest.CaptureFixture[str]
) -> None:
    installed.stub("extra", tools={"secret": plugin_tool})
    assert cli.main(["secret", "read", "a/b/c"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "tbhbdev: plugin 'extra' defines the command 'secret', "
        "which the built-in commands already define\n"
    )


def test_a_plugin_that_fails_to_load_leaves_the_built_ins_working(
    installed: Installed, capsys: pytest.CaptureFixture[str]
) -> None:
    installed.add("broken", ImportError("no module named 'nowhere'"))
    assert cli.main(["--help"]) == 0
    captured = capsys.readouterr()
    assert captured.out == USAGE
    assert captured.err == (
        "tbhbdev: cannot load plugin 'broken': ImportError: no module named 'nowhere'\n"
    )
