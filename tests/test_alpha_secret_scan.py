# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the secret scan of PR text, against the pinned gitleaks."""

import ast
import os
import shutil
import tomllib
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
FIX = "Install it with the host's provisioning tooling."


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


def scan(
    text: str, root: Path, tool: str | None = None, visibility: str = "PUBLIC"
) -> None:
    secret_scan.scan(
        text,
        "the text",
        root=root,
        gitleaks=tool or gitleaks(),
        visibility=visibility,
    )


def refusal(
    text: str, root: Path, tool: str | None = None, visibility: str = "PUBLIC"
) -> str:
    with pytest.raises(SystemExit) as info:
        scan(text, root, tool, visibility)
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


# A stand-in private config with one rule of each family that matters: names
# the scan skips in a private repository, and rules it keeps. The kept
# op-item-1 rule carries a rule allowlist, a bool, an int, a list and
# non-ASCII text, so the copy has to carry every kind of value across.
FAMILIES = (
    PRIVATE_CONFIG
    + r"""
[[rules]]
id = "tailnet-host-1"
description = "A host name"
regex = '''fakehost-[a-z]{3}'''

[[rules]]
id = "private-repo-12"
description = "A private repository name"
regex = '''fake-sibling-repo'''

[[rules]]
id = "tailnet-ipv4-1"
description = "A tailnet address"
regex = '''\b100\.64\.[0-9]+\.[0-9]+\b'''

[[rules]]
id = "op-reference-1"
description = "An op reference"
regex = '''op://vault-one/[a-z]+'''

[[rules]]
id = "op-vault-1"
description = "A vault name"
regex = '''fake-vault'''

[[rules]]
id = "op-item-1"
description = "An item \\ \"quoted\" café 😀"
regex = '''fake-item-[a-z]+'''
keywords = ["fake-item-"]
secretGroup = 0
skipReport = false

[[rules.allowlists]]
description = "One allowed item"
regexes = ['''fake-item-ok''']

[[allowlists]]
description = "A global allowlist"
paths = ['''^skipped/''']

[[rules]]
id = "tool-dir-1"
description = "A tool directory"
regex = '''fake-tool-dir'''
"""
)
DEFAULTS = "[extend]\nuseDefault = true\n"
KEPT = [
    ("100.64.1.2", "tailnet-ipv4-1"),
    ("op://vault-one/item", "op-reference-1"),
    ("fake-vault", "op-vault-1"),
    ("fake-item-bad", "op-item-1"),
    (CANARY_HIT, "tbhb-private-canary"),
]
SKIPPED = ["fakehost-abc", "fake-sibling-repo", "fake-tool-dir"]


@pytest.fixture
def families(root: Path) -> Path:
    """The root, with the private config swapped for the one with families."""
    write_private_config(Path.home(), FAMILIES)
    return root


@pytest.mark.parametrize("text", SKIPPED)
def test_a_private_repo_skips_the_name_rules(families: Path, text: str) -> None:
    scan(f"fix: x\n\nIt names {text}.\n", families, visibility="PRIVATE")


@pytest.mark.parametrize(("text", "rule"), KEPT)
def test_a_private_repo_keeps_every_other_rule(
    families: Path, text: str, rule: str
) -> None:
    assert refusal(f"It names {text}.\n", families, visibility="PRIVATE") == (
        f"refusing the text: gitleaks finds a secret: rule {rule} on line 1"
    )


def test_a_private_repo_keeps_a_kept_rules_allowlist(families: Path) -> None:
    scan("It names fake-item-ok.\n", families, visibility="PRIVATE")


@pytest.mark.parametrize("visibility", ["PUBLIC", "INTERNAL", "", "weird", "private"])
@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("fakehost-abc", "tailnet-host-1"),
        ("fake-sibling-repo", "private-repo-12"),
        ("fake-tool-dir", "tool-dir-1"),
    ],
)
def test_any_other_visibility_scans_under_the_full_config(
    families: Path, visibility: str, text: str, rule: str
) -> None:
    assert refusal(f"It names {text}.\n", families, visibility=visibility) == (
        f"refusing the text: gitleaks finds a secret: rule {rule} on line 1"
    )


def test_a_private_repo_keeps_the_committed_config_and_the_defaults(
    families: Path,
) -> None:
    (families / ".gitleaks.toml").write_text(DEFAULTS + COMMITTED)
    # Split so this file carries no literal that the repository's own gate flags.
    key = "AKIA" + "QXZ7Q3N5R2K4M6PT"
    assert refusal(f"The key is {key}.\n", families, visibility="PRIVATE") == (
        "refusing the text: gitleaks finds a secret: rule aws-access-token on line 1"
    )
    assert refusal(f"{COMMITTED_HIT}\n", families, visibility="PRIVATE") == (
        "refusing the text: gitleaks finds a secret: rule committed-rule on line 1"
    )


def test_the_copy_is_private_holds_no_name_rule_and_is_gone_afterwards(
    families: Path, tmp_path: Path
) -> None:
    fake = tmp_path / "gitleaks"
    log = tmp_path / "log"
    body = (
        "import os, stat, sys\n"
        "path = sys.argv[sys.argv.index('--config') + 1]\n"
        "modes = [stat.S_IMODE(os.stat(p).st_mode) "
        "for p in (path, os.path.dirname(path))]\n"
        f"open({str(log)!r}, 'w').write(repr((path, modes, open(path).read())))\n"
        "sys.stdout.write('[]')\n"
    )
    write_fake(fake, body)
    scan("text\n", families, str(fake), "PRIVATE")
    path, modes, text = ast.literal_eval(log.read_text())
    assert modes == [0o600, 0o700]
    assert path != str(secret_scan.private_config())
    assert Path(path).name == "private.toml"
    assert not Path(path).exists()
    assert not Path(path).parent.exists()
    ids = [rule["id"] for rule in tomllib.loads(text)["rules"]]
    assert ids == ["tbhb-private-canary", "tailnet-ipv4-1", "op-reference-1",
                   "op-vault-1", "op-item-1"]  # fmt: skip


def test_the_copy_is_gone_after_a_refusal(families: Path, tmp_path: Path) -> None:
    fake = tmp_path / "gitleaks"
    log = tmp_path / "log"
    body = (
        "import sys\n"
        f"open({str(log)!r}, 'w').write(sys.argv[sys.argv.index('--config') + 1])\n"
        "sys.stdout.write('[]')\n"
        "sys.stderr.write('boom')\n"
        "sys.exit(3)\n"
    )
    write_fake(fake, body)
    assert refusal("text\n", families, str(fake), "PRIVATE") == (
        "cannot scan the text for secrets: gitleaks failed (exit 3): boom"
    )
    assert not Path(log.read_text()).exists()


def test_the_copy_parses_back_to_the_private_config_minus_the_name_rules() -> None:
    expected = tomllib.loads(FAMILIES)
    expected["rules"] = [
        rule
        for rule in expected["rules"]
        if rule["id"] not in {"tailnet-host-1", "private-repo-12", "tool-dir-1"}
    ]
    copy = secret_scan.without_name_rules(FAMILIES)
    assert copy is not None
    assert tomllib.loads(copy) == expected
    assert expected["rules"][-1]["allowlists"] == [
        {"description": "One allowed item", "regexes": ["fake-item-ok"]}
    ]
    assert "\\U0001F600" not in copy
    assert "\U0001f600" in copy


@pytest.mark.parametrize(
    ("rule_id", "skipped"),
    [
        ("private-repo-1", True),
        ("tailnet-host-11", True),
        ("tailnet-stem-2", True),
        ("tailnet-name-1", True),
        ("tailnet-suffix-1", True),
        ("local-name-1", True),
        ("home-path-2", True),
        ("tool-dir-1", True),
        ("private-repo", True),
        ("tailnet-ipv4-1", False),
        ("tailnet-ipv6-1", False),
        ("private-ipv4-3", False),
        ("mac-address-1", False),
        ("op-vault-4", False),
        ("endpoint-1", False),
        ("keychain-2", False),
        ("tbhb-private-canary", False),
        ("private-repo-x", False),
        ("x-private-repo-1", False),
        ("private-repo-1-x", False),
    ],
)
def test_the_name_rule_families_are_the_ids_without_their_number(
    rule_id: str, skipped: bool
) -> None:
    assert secret_scan.is_name_rule({"id": rule_id}) is skipped


def test_a_rule_without_an_id_is_not_a_name_rule() -> None:
    assert secret_scan.is_name_rule({}) is False


def test_a_private_config_without_rules_gets_an_empty_rule_list() -> None:
    copy = secret_scan.without_name_rules("title = 'x'\n")
    assert copy is not None
    assert tomllib.loads(copy) == {"title": "x", "rules": []}


def test_a_private_config_that_is_not_toml_is_left_to_gitleaks(root: Path) -> None:
    write_private_config(Path.home(), PRIVATE_CONFIG + "\nrules = [\n")
    message = refusal("clean\n", root, visibility="PRIVATE")
    assert message.startswith(
        "cannot scan the text for secrets: gitleaks failed (exit 1): "
    )
