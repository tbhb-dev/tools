# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the secret scan of PR text, against the pinned gitleaks."""

import ast
import os
import shutil
from pathlib import Path

import pytest
from fake_tool import PRIVATE_CONFIG, write_fake, write_private_config

from tbhbdev.alpha import secret_scan

# A committed config with one rule of its own and none of gitleaks' defaults,
# so each finding below comes from exactly one of the two configs.
COMMITTED = """[[rules]]
id = "committed-rule"
description = "Test committed rule"
regex = '''committed-secret-[0-9]{8}'''
"""
CANARY_HIT = "test-canary-0123456789abcdef"
COMMITTED_HIT = "committed-secret-12345678"
FIX = "Run `tbhbdev host upgrade` (step gitleaks-config), which writes it."


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository root with the committed config, and HOME with the private one."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    write_private_config(home)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitleaks.toml").write_text(COMMITTED)
    return repo


def gitleaks() -> str:
    found = shutil.which("gitleaks")
    assert found, "the pinned gitleaks must be on PATH"
    return found


def scan(text: str, root: Path, tool: str | None = None) -> None:
    secret_scan.scan(text, "the text", root=root, gitleaks=tool or gitleaks())


def refusal(text: str, root: Path, tool: str | None = None) -> str:
    with pytest.raises(SystemExit) as info:
        scan(text, root, tool)
    return str(info.value)


def test_clean_text_passes(root: Path) -> None:
    scan("fix: nothing secret here\n\nJust prose.\n", root)


@pytest.mark.parametrize(
    ("hit", "rule"),
    [(CANARY_HIT, "tbhb-private-canary"), (COMMITTED_HIT, "committed-rule")],
)
def test_a_finding_from_either_config_is_refused_by_rule_and_line(
    root: Path, hit: str, rule: str
) -> None:
    message = refusal(f"fix: x\n\n## Why\n\nThe value is {hit}.\n", root)
    assert message == (
        f"refusing the text: gitleaks finds a secret: rule {rule} on line 5"
    )


def test_every_finding_is_listed(root: Path) -> None:
    message = refusal(f"{COMMITTED_HIT}\nclean\n{CANARY_HIT}\n", root)
    assert message == (
        "refusing the text: gitleaks finds a secret: rule committed-rule on line 1; "
        "rule tbhb-private-canary on line 3"
    )


def test_the_private_config_path_is_under_home(root: Path) -> None:
    del root
    assert secret_scan.private_config() == (
        Path.home() / ".config" / "gitleaks" / "private.toml"
    )


def test_a_missing_private_config_names_the_path_and_the_fix(root: Path) -> None:
    secret_scan.private_config().unlink()
    assert refusal("clean\n", root) == (
        "cannot scan the text for secrets: the private gitleaks config "
        f"{secret_scan.private_config()} cannot be read (No such file or "
        f"directory). {FIX}"
    )


def test_a_private_config_without_the_canary_is_refused(root: Path) -> None:
    write_private_config(Path.home(), PRIVATE_CONFIG.replace("tbhb-private", "x"))
    assert refusal("clean\n", root) == (
        f"cannot scan the text for secrets: {secret_scan.private_config()} has no "
        "rule with the id tbhb-private-canary, so it is not the private gitleaks "
        f"config. {FIX}"
    )


def test_a_missing_committed_config_is_refused(root: Path) -> None:
    (root / ".gitleaks.toml").unlink()
    assert refusal("clean\n", root) == (
        f"cannot scan the text for secrets: {root / '.gitleaks.toml'} does not exist"
    )


def test_gitleaks_failing_is_refused_with_its_log_and_no_color(root: Path) -> None:
    (root / ".gitleaks.toml").write_text("rules = [\n")
    message = refusal("clean\n", root)
    assert message.startswith(
        "cannot scan the text for secrets: gitleaks failed (exit 1): "
    )
    assert "failed to load extended config" in message
    assert "\x1b" not in message


@pytest.mark.parametrize(
    ("rc", "stdout"), [(0, "not json"), (0, "{}"), (2, "[]"), (2, "")]
)
def test_a_report_that_is_not_a_clean_scan_is_refused(
    root: Path, tmp_path: Path, rc: int, stdout: str
) -> None:
    fake = tmp_path / "gitleaks"
    body = (
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        "sys.stderr.write('bad\\n  config\\n')\n"
        f"sys.exit({rc})\n"
    )
    write_fake(fake, body)
    assert refusal("clean\n", root, str(fake)) == (
        f"cannot scan the text for secrets: gitleaks failed (exit {rc}): bad config"
    )


def test_gitleaks_runs_from_the_root_with_the_private_config(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan must set NO_COLOR itself, not inherit it from the test runner.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("SECRET_SCAN_PASSED", "yes")
    fake = tmp_path / "gitleaks"
    log = tmp_path / "log"
    body = (
        "import os, sys\n"
        "env = [os.environ.get(k) for k in ('NO_COLOR', 'SECRET_SCAN_PASSED')]\n"
        f"open({str(log)!r}, 'w').write("
        "repr((sys.argv[1:], os.getcwd(), sys.stdin.read(), env)))\n"
        "sys.stdout.write('[]')\n"
    )
    write_fake(fake, body)
    scan("text\n", root, str(fake))
    argv = ["stdin", "--redact", "--no-banner", "--config"]
    argv += [str(secret_scan.private_config()), "--report-format", "json"]
    argv += ["--report-path", "-"]
    assert ast.literal_eval(log.read_text()) == (
        argv,
        os.path.realpath(root),
        "text\n",
        ["1", "yes"],
    )


def test_findings_reads_rule_and_line() -> None:
    report = '[{"RuleID": "r", "StartLine": 4, "Secret": "REDACTED"}]'
    assert secret_scan.findings(report) == [(4, "r")]
    assert secret_scan.findings("[]") == []
    assert secret_scan.findings("{}") is None
    assert secret_scan.findings("") is None
