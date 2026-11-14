# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the pre-commit scan of staged changes, against the pinned gitleaks."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fake_tool import (
    PRIVATE_CONFIG,
    PRIVATE_REPO,
    break_host_names,
    pythonpath,
    write_host_names,
    write_private_config,
)

from tbhbdev import alpha
from tbhbdev.alpha import install, pre_commit_scan, secret_scan

# A committed config with one rule of its own and none of gitleaks' defaults.
COMMITTED = """[[rules]]
id = "committed-rule"
description = "Test committed rule"
regex = '''committed-secret-[0-9]{8}'''
"""
# A private config with a tailnet address rule, which a private repository
# keeps, and a host name rule, which it skips.
PRIVATE = (
    PRIVATE_CONFIG
    + r"""
[[rules]]
id = "tailnet-ipv4-1"
description = "A tailnet address"
regex = '''\b100\.64\.[0-9]+\.[0-9]+\b'''

[[rules]]
id = "tailnet-host-1"
description = "A host name"
regex = '''fakehost-[a-z]{3}'''
"""
)
# Built from parts, so this file holds no literal address for the hook to flag.
ADDRESS = ".".join(map(str, (100, 64, 7, 9)))
HOST = "fakehost-" + "abc"
# An invented name with a private component, built so no line spells one.
SUFFIXED = "fixture" + "-private"
PUBLIC = "https://github.com/tbhb-dev/example.git"
FIX = "Install it with the host's provisioning tooling."
TAILNET = (
    "pre-commit: refusing the staged changes: gitleaks finds a secret: "
    "rule tailnet-ipv4-1 in README.md:2\n"
)
NAMED = (
    "pre-commit: refusing the staged changes: gitleaks finds a secret: "
    "rule tailnet-host-1 in notes.txt:1\n"
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A public tbhb-dev work tree with the committed config, as the cwd."""
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example@example.test")
    for name in ("HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    write_private_config(Path.home(), PRIVATE)
    write_host_names(Path.home())
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "remote", "add", "origin", PUBLIC)
    (path / ".gitleaks.toml").write_text(COMMITTED)
    git(path, "add", ".gitleaks.toml")
    git(path, "commit", "-qm", "initial")
    monkeypatch.chdir(path)
    return path


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)


def run(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    status = pre_commit_scan.main([], "tbhbdev alpha pre-commit-scan")
    out, err = capsys.readouterr()
    return status, out, err


def test_an_ordinary_staged_file_passes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage(repo, "README.md", "# Example\n\nNothing private here.\n")
    assert run(capsys) == (0, "", "")


def test_a_staged_tailnet_address_is_refused_by_rule_file_and_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    status, out, err = run(capsys)
    assert (status, out, err) == (1, "", TAILNET)
    assert ADDRESS not in out + err


def test_every_finding_is_listed_in_file_order(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage(repo, "b.txt", f"{ADDRESS}\n")
    stage(repo, "a.txt", "clean\ncommitted-secret-12345678\n")
    assert run(capsys) == (
        1,
        "",
        (
            "pre-commit: refusing the staged changes: gitleaks finds a secret: "
            "rule committed-rule in a.txt:2; rule tailnet-ipv4-1 in b.txt:1\n"
        ),
    )


def test_only_staged_additions_count(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "old.txt").write_text(f"{ADDRESS}\n")
    git(repo, "add", "old.txt")
    git(repo, "commit", "-qm", "old")
    stage(repo, "README.md", "clean\n")
    (repo / "README.md").write_text(f"clean\n{ADDRESS}\n")  # not staged
    assert run(capsys) == (0, "", "")


def test_a_public_origin_gets_the_name_rules(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del repo
    stage(Path.cwd(), "notes.txt", f"It names {HOST}.\n")
    assert run(capsys) == (1, "", NAMED)


@pytest.mark.parametrize(
    "origin",
    [
        f"git@github.com:tbhb-dev/{PRIVATE_REPO}.git",
        f"https://github.com/tbhb-dev/{SUFFIXED}",
    ],
)
def test_a_private_origin_skips_the_name_rules_and_keeps_the_rest(
    repo: Path, capsys: pytest.CaptureFixture[str], origin: str
) -> None:
    git(repo, "remote", "set-url", "origin", origin)
    stage(repo, "notes.txt", f"It names {HOST}.\n")
    assert pre_commit_scan.visibility() == secret_scan.PRIVATE
    assert run(capsys) == (0, "", "")
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    assert run(capsys) == (1, "", TAILNET)


def test_an_origin_listed_in_the_user_config_is_private(repo: Path) -> None:
    git(repo, "remote", "set-url", "origin", "https://github.com/tbhb-dev/listed")
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "tbhbdev" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[pr_check]\nprivate_repos = ["listed"]\n')
    assert pre_commit_scan.visibility() == secret_scan.PRIVATE


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_an_unreadable_host_list_scans_under_the_full_config(
    repo: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    git(repo, "remote", "set-url", "origin", f"https://github.com/o/{PRIVATE_REPO}")
    break_host_names(Path.home() / ".local/state/tbhb/private-repos.txt", kind)
    assert pre_commit_scan.visibility() == ""
    stage(repo, "notes.txt", f"It names {HOST}.\n")
    assert run(capsys) == (1, "", NAMED)


def test_no_origin_scans_under_the_full_config(repo: Path) -> None:
    git(repo, "remote", "remove", "origin")
    assert pre_commit_scan.visibility() == ""


def test_a_missing_private_config_refuses_the_commit(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage(repo, "README.md", "clean\n")
    secret_scan.private_config().unlink()
    assert run(capsys) == (
        1,
        "",
        (
            "pre-commit: cannot scan the staged changes for secrets: the private "
            f"gitleaks config {secret_scan.private_config()} cannot be read (No such "
            f"file or directory). {FIX}\n"
        ),
    )


def test_a_private_config_without_the_canary_refuses_the_commit(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage(repo, "README.md", "clean\n")
    write_private_config(Path.home(), PRIVATE.replace(secret_scan.GITLEAKS_CANARY, "x"))
    assert run(capsys) == (
        1,
        "",
        (
            "pre-commit: cannot scan the staged changes for secrets: "
            f"{secret_scan.private_config()} has no rule with the id "
            f"tbhb-private-canary, so it is not the private gitleaks config. {FIX}\n"
        ),
    )


def test_a_missing_committed_config_refuses_the_commit(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "rm", "-q", ".gitleaks.toml")
    assert run(capsys) == (
        1,
        "",
        (
            "pre-commit: cannot scan the staged changes for secrets: "
            f"{repo / '.gitleaks.toml'} does not exist\n"
        ),
    )


def test_a_gitleaks_failure_refuses_the_commit_without_the_secret(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".gitleaks.toml").write_text("rules = [\n")
    stage(repo, "README.md", f"{ADDRESS}\n")
    status, out, err = run(capsys)
    assert (status, out) == (1, "")
    assert err.startswith(
        "pre-commit: cannot scan the staged changes for secrets: gitleaks failed "
        "(exit 1): "
    )
    assert ADDRESS not in err


def test_without_gitleaks_the_commit_is_refused(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stage(repo, "README.md", "clean\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "git").symlink_to(shutil.which("git") or "git")
    monkeypatch.setenv("PATH", str(empty))
    assert run(capsys) == (
        1,
        "",
        (
            "pre-commit: gitleaks not found on PATH or through `mise which`; "
            "run `mise install`\n"
        ),
    )


def test_arguments_are_a_usage_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del repo
    assert pre_commit_scan.main(["x"], "p") == 2
    assert capsys.readouterr() == ("", "usage: p\n")


def test_alpha_runs_it(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del repo
    assert alpha.COMMANDS["pre-commit-scan"] is alpha.pre_commit_scan_command
    assert "\n  pre-commit-scan\n" in alpha.USAGE
    assert alpha.main(["pre-commit-scan", "x"], "tbhbdev alpha") == 2
    assert capsys.readouterr().err == "usage: tbhbdev alpha pre-commit-scan\n"


def commit_all(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """A real `git commit -a` through the installed pre-commit shim."""
    return subprocess.run(
        ["git", "commit", "-q", "-a", "-m", "x"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize("linked", [False, True])
def test_the_installed_shim_scans_what_commit_a_records(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, linked: bool
) -> None:
    """`git commit -a` stages through GIT_INDEX_FILE, which gitleaks must see.

    A linked worktree's hooks also run with GIT_DIR set, which root() and
    gitleaks' own git diff must both follow.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    # Not write_fake: its tbhbdev.py beside the stub would shadow the package.
    tool = tools / "tbhbdev"
    tool.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m tbhbdev "$@"\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", pythonpath(Path(install.__file__).parents[2]))
    env = dict(os.environ)
    env.pop("MUTANT_UNDER_TEST", None)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    no_prek = [sys.executable, "-c", "raise SystemExit(1)"]
    install.install_identity_hook(
        no_prek, repo, hooks, ("pre-commit", "pre-commit-scan")
    )
    stage(repo, "README.md", "clean\n")
    git(repo, "commit", "-qm", "readme")
    tree = repo
    if linked:
        tree = tmp_path / "tree"
        git(repo, "worktree", "add", "-q", "-b", "tree", str(tree))
    (tree / "README.md").write_text(f"clean\n{ADDRESS}\n")
    done = commit_all(tree, env)
    assert (done.returncode, done.stdout, done.stderr) == (1, "", TAILNET)
    assert git(tree, "rev-list", "--count", "HEAD") == "2"
    (tree / "README.md").write_text("still clean\n")
    done = commit_all(tree, env)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert git(tree, "rev-list", "--count", "HEAD") == "3"
