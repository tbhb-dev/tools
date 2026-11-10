"""Tests for `tbhbdev alpha`'s dispatch to its commands."""

import io
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from tbhbdev import alpha, cli
from tbhbdev.alpha import (
    close,
    guard_gh_pr,
    identity,
    install,
    pull_request,
    squash_merge,
)

USAGE = alpha.USAGE.format(prog="tbhbdev alpha")


def test_alpha_is_a_tool() -> None:
    assert cli.TOOLS["alpha"] is alpha.main


def test_usage_says_the_commands_are_interim() -> None:
    assert "interim: they may change or go away without notice" in USAGE
    for name in ("identity", "guard-gh-pr", "pr", "issue", "merge", "install"):
        assert f"\n  {name} " in USAGE


def test_no_arguments_prints_usage_to_stderr_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert alpha.main([], "tbhbdev alpha") == 2
    assert capsys.readouterr() == ("", USAGE)


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_to_stdout(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert alpha.main([flag, "ignored"], "tbhbdev alpha") == 0
    assert capsys.readouterr() == (USAGE, "")


def test_unknown_command_fails_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert alpha.main(["nope", "x"], "tbhbdev alpha") == 2
    assert capsys.readouterr() == (
        "",
        f"tbhbdev alpha: unknown command 'nope'\n{USAGE}",
    )


def test_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    def run(argv: list[str], env: object) -> int:
        calls.append(("identity", (argv, env)))
        return 3

    def guard() -> int:
        calls.append(("guard", None))
        return 4

    def fake(name: str, code: int) -> object:
        def main(argv: list[str], prog: str) -> int:
            calls.append((name, (argv, prog)))
            return code

        return main

    monkeypatch.setattr(identity, "run", run)
    monkeypatch.setattr(guard_gh_pr, "main", guard)
    monkeypatch.setattr(pull_request, "main", fake("pr", 5))
    monkeypatch.setattr(squash_merge, "main", fake("merge", 6))
    monkeypatch.setattr(install, "main", fake("install", 7))
    monkeypatch.setattr(close, "main", fake("close", 8))
    assert alpha.main(["identity", "check", "m"], "p") == 3
    assert alpha.main(["guard-gh-pr", "x"], "p") == 4
    assert alpha.main(["pr", "check"], "p") == 5
    assert alpha.main(["merge", "3"], "p") == 6
    assert alpha.main(["install", "--dry-run"], "p") == 7
    assert alpha.main(["pr", "close", "3", "--reason", "r"], "p") == 8
    assert alpha.main(["issue", "close", "3"], "p") == 8
    assert calls == [
        ("identity", (["check", "m"], os.environ)),
        ("guard", None),
        ("pr", (["check"], "p pr")),
        ("merge", (["3"], "p merge")),
        ("install", (["--dry-run"], "p install")),
        ("close", (["pr", "close", "3", "--reason", "r"], "p pr")),
        ("close", (["issue", "close", "3"], "p issue")),
    ]


def test_guard_runs_from_the_top(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({})))
    assert cli.main(["alpha", "guard-gh-pr"]) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("command", ["state-guard", "session-context"])
def test_harness_hooks_run_from_the_top_without_config(
    command: str,
    config_file: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert config_file is not None
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({})))
    assert cli.main(["alpha", command]) == 0
    assert capsys.readouterr() == ("", "")


def test_pre_push_checks_spelling_and_prose() -> None:
    root = Path(__file__).resolve().parents[1]
    hooks = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    assert "pre-push" in hooks["default_install_hook_types"]
    prose = next(
        hook
        for repo in hooks["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "spelling-and-prose"
    )
    assert prose == {
        "id": "spelling-and-prose",
        "name": "cspell and vale",
        "language": "system",
        "entry": "bazelisk test //:cspell_test //:vale_test",
        "stages": ["pre-push"],
        "always_run": True,
        "pass_filenames": False,
    }
