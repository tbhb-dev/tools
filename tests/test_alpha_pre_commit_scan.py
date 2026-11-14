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
    write_fake,
    write_host_names,
    write_private_config,
)

from tbhbdev import alpha
from tbhbdev.alpha import install, pre_commit_scan, private_names, secret_scan

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
description = "A tailnet address fixture"
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
        f"https://github.com/tbhb-dev/{PRIVATE_REPO.upper()}",
    ],
)
def test_a_listed_tbhb_dev_origin_skips_the_name_rules_and_keeps_the_rest(
    repo: Path, capsys: pytest.CaptureFixture[str], origin: str
) -> None:
    git(repo, "remote", "set-url", "origin", origin)
    stage(repo, "notes.txt", f"It names {HOST}.\n")
    assert pre_commit_scan.visibility() == secret_scan.PRIVATE
    assert run(capsys) == (0, "", "")
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    assert run(capsys) == (1, "", TAILNET)


def test_a_path_only_word_on_the_host_list_is_listed(repo: Path) -> None:
    listed = write_host_names(Path.home())
    listed.write_text(f"{listed.read_text()}{private_names.PATH_HEADER} sidecar\n")
    git(repo, "remote", "set-url", "origin", "https://github.com/tbhb-dev/sidecar")
    assert pre_commit_scan.visibility() == secret_scan.PRIVATE


@pytest.mark.parametrize(
    "origin",
    [
        # A -private component alone, without the host list naming it.
        f"https://github.com/tbhb-dev/{SUFFIXED}",
        # Another owner, though the host list names the repository.
        f"https://github.com/someone/{PRIVATE_REPO}",
        f"git@github.com:tbhb-dev-evil/{PRIVATE_REPO}.git",
        # Origins that don't parse as a tbhb-dev repository.
        "not a url",
        f"https://github.com/tbhb-dev/{PRIVATE_REPO}/extra",
        "",
    ],
)
def test_any_other_origin_keeps_the_name_rules(
    repo: Path, capsys: pytest.CaptureFixture[str], origin: str
) -> None:
    git(repo, "config", "remote.origin.url", origin)
    assert pre_commit_scan.visibility() == ""
    stage(repo, "notes.txt", f"It names {HOST}.\n")
    assert run(capsys) == (1, "", NAMED)


def test_the_user_configs_list_does_not_skip_the_name_rules(repo: Path) -> None:
    git(repo, "remote", "set-url", "origin", "https://github.com/tbhb-dev/listed")
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "tbhbdev" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[pr_check]\nprivate_repos = ["listed"]\n')
    assert pre_commit_scan.visibility() == ""


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_an_unreadable_host_list_scans_under_the_full_config(
    repo: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        f"https://github.com/tbhb-dev/{PRIVATE_REPO}",
    )
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
            f"{secret_scan.private_config()} does not parse to rules with the id "
            f"{secret_scan.GITLEAKS_CANARY} and at least one other, so it is not the "
            "private "
            f"gitleaks config. {FIX}\n"
        ),
    )


def test_the_committed_config_comes_from_the_index(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unstaged edit to .gitleaks.toml can't loosen the scan; a staged one can."""
    allow = f"{COMMITTED}\n[allowlist]\nregexes = ['{ADDRESS.replace('.', '[.]')}']\n"
    (repo / ".gitleaks.toml").write_text(allow)
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    assert run(capsys) == (1, "", TAILNET)
    git(repo, "add", ".gitleaks.toml")
    assert run(capsys) == (0, "", "")


def test_without_a_committed_config_in_the_index_the_defaults_apply(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The private config extends gitleaks' defaults; the work tree file is unread."""
    git(repo, "rm", "-q", "--cached", ".gitleaks.toml")
    stage(repo, "a.txt", "committed-secret-12345678\n")
    assert run(capsys) == (0, "", "")
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    assert run(capsys) == (1, "", TAILNET)
    git(repo, "rm", "-q", "--cached", "README.md")
    # Split so this file carries no literal that the repository's own gate flags.
    stage(repo, "key.txt", "AKIA" + "QXZ7Q3N5R2K4M6PT\n")
    assert run(capsys) == (
        1,
        "",
        (
            "pre-commit: refusing the staged changes: gitleaks finds a secret: "
            "rule aws-access-token in key.txt:1\n"
        ),
    )


@pytest.mark.parametrize(
    "answers", [[(128, "")], [(1, "abc")], [(0, "abc"), (128, "")]]
)
def test_an_index_git_cannot_read_refuses_the_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[int, str]]
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        assert cwd == repo
        calls.append(args)
        return answers[len(calls) - 1]

    monkeypatch.setattr(secret_scan, "git", fake_git)
    with pytest.raises(SystemExit) as raised:
        secret_scan.staged_file(repo, ".gitleaks.toml", "the staged changes")
    assert str(raised.value) == (
        "cannot scan the staged changes for secrets: git cannot read "
        ".gitleaks.toml from the index"
    )
    assert calls[0] == ("rev-parse", "--verify", "--quiet", ":.gitleaks.toml")
    assert calls[1:] == [("cat-file", "blob", "abc")][: len(answers) - 1]


def test_the_ignore_file_comes_from_the_index(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An untracked .gitleaksignore can't hide a finding; a staged one can."""
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    (repo / ".gitleaksignore").write_text("README.md:tailnet-ipv4-1:2\n")
    assert run(capsys) == (1, "", TAILNET)
    git(repo, "add", ".gitleaksignore")
    assert run(capsys) == (0, "", "")


def test_a_repository_git_cannot_find_refuses_the_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        assert (args, cwd) == (("rev-parse", "--absolute-git-dir"), repo)
        return 128, ""

    monkeypatch.setattr(secret_scan, "git", fake_git)
    with pytest.raises(SystemExit) as raised:
        secret_scan.staged_env(repo, "the staged changes")
    assert str(raised.value) == (
        "cannot scan the staged changes for secrets: git cannot find the repository"
    )


@pytest.mark.parametrize("index", ["", "relative.index", "/abs/index"])
def test_gitleaks_git_finds_the_repository_and_the_hooks_index(
    repo: Path, monkeypatch: pytest.MonkeyPatch, index: str
) -> None:
    if index:
        monkeypatch.setenv("GIT_INDEX_FILE", index)
    env = secret_scan.staged_env(repo, "the staged changes")
    assert env["GIT_DIR"] == str((repo / ".git").resolve())
    assert env["GIT_WORK_TREE"] == str(repo)
    expected = {"": None, "relative.index": str(Path.cwd() / "relative.index")}
    assert env.get("GIT_INDEX_FILE") == expected.get(index, index)


def test_a_gitleaks_failure_refuses_the_commit_with_its_exit_status_only(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stage(repo, ".gitleaks.toml", "rules = [\n")
    stage(repo, "README.md", f"{ADDRESS}\n")
    assert run(capsys) == (
        1,
        "",
        (
            "pre-commit: cannot scan the staged changes for secrets: gitleaks failed "
            "(exit 1)\n"
        ),
    )


def test_a_private_regex_gitleaks_rejects_never_reaches_the_output(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    regex = "fakehost-" + "(unclosed"
    rule = f"\n[[rules]]\nid = \"tailnet-host-9\"\nregex = '{regex}'\n"
    write_private_config(Path.home(), PRIVATE + rule)
    stage(repo, "README.md", "clean\n")
    status, out, err = run(capsys)
    assert (status, out) == (1, "")
    assert err.startswith(
        "pre-commit: cannot scan the staged changes for secrets: gitleaks failed (exit "
    )
    assert "fakehost" not in err


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


def hook_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The environment for real commits: `tbhbdev` on PATH, running this source."""
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
    return env


# prek that finds no config: `prek list` fails, so nothing is chained.
NO_PREK = [sys.executable, "-c", "raise SystemExit(1)"]


def install_scan(repo: Path, prek: list[str], name: str = "pre-commit") -> Path:
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    install.install_identity_hook(prek, repo, hooks, (name, "pre-commit-scan"))
    return hooks


@pytest.mark.parametrize("linked", [False, True])
def test_the_installed_shim_scans_what_commit_a_records(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, linked: bool
) -> None:
    """`git commit -a` stages through GIT_INDEX_FILE, which gitleaks must see.

    A linked worktree's hooks also run with GIT_DIR set, which root() and
    gitleaks' own git diff must both follow.
    """
    env = hook_env(tmp_path, monkeypatch)
    install_scan(repo, NO_PREK)
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


needs_prek = pytest.mark.skipif(shutil.which("prek") is None, reason="needs prek")
# A foreign pre-commit hook that stages a secret after the scan would have run,
# and leaves a mark that it ran.
STAGES_A_LEAK = """#!/bin/sh
echo ran >> "$(git rev-parse --git-dir)/foreign.log"
printf 'clean\\n%s\\n' "$LEAK" > leak.txt
git add leak.txt
"""
LEAKED = (
    "pre-commit: refusing the staged changes: gitleaks finds a secret: "
    "rule tailnet-ipv4-1 in leak.txt:2\n"
)
PREK_CONFIG = """repos:
- repo: local
  hooks:
  - id: mark
    name: mark
    entry: sh -c 'echo prek >> "$(git rev-parse --git-dir)/foreign.log"'
    language: system
    pass_filenames: false
    always_run: true
"""


def commit(
    repo: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def foreign_log(repo: Path) -> list[str]:
    log = repo / ".git" / "foreign.log"
    return log.read_text().split() if log.exists() else []


def foreign_hook(repo: Path, body: str = STAGES_A_LEAK) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(body)
    hook.chmod(0o755)


@pytest.mark.parametrize(
    "prek",
    [
        pytest.param(NO_PREK, id="fake-no-prek"),
        pytest.param(["prek"], id="real-prek-no-config", marks=needs_prek),
    ],
)
def test_a_secret_a_moved_hook_stages_is_scanned_after_it(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prek: list[str]
) -> None:
    """Without prek hooks, the shim runs the moved hook, then scans its index."""
    env = hook_env(tmp_path, monkeypatch) | {"LEAK": ADDRESS}
    foreign_hook(repo)
    hooks = install_scan(repo, prek)
    assert not (hooks / "pre-commit.prek").exists()
    stage(repo, "README.md", "clean\n")
    done = commit(repo, env, "commit", "-qm", "x")
    assert (done.returncode, done.stdout, done.stderr) == (1, "", LEAKED)
    assert foreign_log(repo) == ["ran"]
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
    done = commit(repo, env | {"LEAK": "nothing"}, "commit", "-qm", "x")
    assert (done.returncode, done.stderr) == (0, "")
    assert foreign_log(repo) == ["ran", "ran"]
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


# A prek config with hooks, but none for pre-commit: prek has nothing to chain.
PRE_PUSH_ONLY = """repos:
- repo: local
  hooks:
  - id: push
    name: push
    entry: "true"
    language: system
    stages: [pre-push]
"""


@needs_prek
def test_a_prek_config_without_hooks_for_the_stage_runs_the_moved_hook_directly(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = hook_env(tmp_path, monkeypatch) | {"LEAK": ADDRESS}
    stage(repo, ".pre-commit-config.yaml", PRE_PUSH_ONLY)
    git(repo, "commit", "-qm", "prek config")
    foreign_hook(repo)
    hooks = install_scan(repo, ["prek"])
    assert not (hooks / "pre-commit.prek").exists()
    stage(repo, "README.md", "clean\n")
    done = commit(repo, env, "commit", "-qm", "x")
    assert (done.returncode, done.stdout, done.stderr) == (1, "", LEAKED)
    done = commit(repo, env | {"LEAK": "nothing"}, "commit", "-qm", "x")
    assert (done.returncode, done.stderr) == (0, "")
    assert foreign_log(repo) == ["ran", "ran"]


@needs_prek
def test_with_prek_hooks_the_chained_hook_runs_the_moved_hook_then_the_scan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With real prek's hooks, prek's hook and the moved one run, then the scan."""
    env = hook_env(tmp_path, monkeypatch) | {"LEAK": ADDRESS}
    stage(repo, ".pre-commit-config.yaml", PREK_CONFIG)
    git(repo, "commit", "-qm", "prek config")
    foreign_hook(repo)
    hooks = install_scan(repo, ["prek"])
    assert (hooks / "pre-commit.prek").is_file()
    assert (hooks / "pre-commit.legacy").read_text() == STAGES_A_LEAK
    stage(repo, "README.md", "clean\n")
    done = commit(repo, env, "commit", "-qm", "x")
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr.endswith(LEAKED)
    assert sorted(foreign_log(repo)) == ["prek", "ran"]
    done = commit(repo, env | {"LEAK": "nothing"}, "commit", "-qm", "x")
    assert done.returncode == 0, done.stderr
    assert git(repo, "rev-list", "--count", "HEAD") == "3"


def test_a_failing_chained_hook_fails_the_commit_before_the_scan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = hook_env(tmp_path, monkeypatch)
    foreign_hook(repo, "#!/bin/sh\necho foreign failed >&2\nexit 3\n")
    install_scan(repo, NO_PREK)
    write_private_config(Path.home(), "not toml")  # the scan would say so
    stage(repo, "README.md", "clean\n")
    done = commit(repo, env, "commit", "-qm", "x")
    assert (done.returncode, done.stderr) == (1, "foreign failed\n")


@pytest.mark.parametrize(
    "prek",
    [
        pytest.param(NO_PREK, id="fake-no-prek"),
        pytest.param(["prek"], id="real-prek", marks=needs_prek),
    ],
)
def test_a_merge_commit_is_scanned_by_the_pre_merge_commit_shim(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prek: list[str]
) -> None:
    env = hook_env(tmp_path, monkeypatch)
    git(repo, "checkout", "-qb", "side")
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    git(repo, "commit", "-qm", "side")
    git(repo, "checkout", "-q", "-")
    stage(repo, "other.txt", "clean\n")
    git(repo, "commit", "-qm", "main")
    install_scan(repo, prek, "pre-merge-commit")
    done = commit(repo, env, "merge", "--no-ff", "-q", "-m", "merge", "side")
    assert (done.returncode, done.stdout, done.stderr) == (
        1,
        "",
        TAILNET + "Not committing merge; use 'git commit' to complete the merge.\n",
    )
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    git(repo, "merge", "--abort")
    git(repo, "checkout", "-q", "side")
    stage(repo, "README.md", "# Example\n")
    git(repo, "commit", "-qm", "fixed")
    git(repo, "checkout", "-q", "-")
    done = commit(repo, env, "merge", "--no-ff", "-q", "-m", "merge", "side")
    assert (done.returncode, done.stderr) == (0, "")


@needs_prek
def test_a_merge_with_prek_hooks_runs_them_then_the_scan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = hook_env(tmp_path, monkeypatch)
    stage(repo, ".pre-commit-config.yaml", PREK_CONFIG)
    git(repo, "commit", "-qm", "prek config")
    git(repo, "checkout", "-qb", "side")
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    git(repo, "commit", "-qm", "side")
    git(repo, "checkout", "-q", "-")
    stage(repo, "other.txt", "clean\n")
    git(repo, "commit", "-qm", "main")
    hooks = install_scan(repo, ["prek"], "pre-merge-commit")
    assert (hooks / "pre-merge-commit.prek").is_file()
    done = commit(repo, env, "merge", "--no-ff", "-q", "-m", "merge", "side")
    assert done.returncode == 1
    assert TAILNET in done.stderr
    assert foreign_log(repo) == ["prek"]
    assert git(repo, "rev-list", "--count", "HEAD") == "3"


FAILING_INSTALL = """import subprocess, sys
if sys.argv[1] == "install":
    print("prek: install failed", file=sys.stderr)
    sys.exit(1)
sys.exit(subprocess.run(["prek", *sys.argv[1:]], check=False).returncode)
"""


@needs_prek
def test_a_failed_prek_install_still_leaves_a_scanning_shim(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With real prek's list: the install fails, the scan still guards commits."""
    env = hook_env(tmp_path, monkeypatch)
    stage(repo, ".pre-commit-config.yaml", PREK_CONFIG)
    git(repo, "commit", "-qm", "prek config")
    fake = tmp_path / "failing" / "prek"
    fake.parent.mkdir()
    write_fake(fake, FAILING_INSTALL)
    with pytest.raises(install.InstallError, match="prek install -f"):
        install_scan(repo, [str(fake)])
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert os.access(hook, os.X_OK)
    assert not (hook.parent / "pre-commit.prek").exists()
    stage(repo, "README.md", f"# Example\nThe node is at {ADDRESS}.\n")
    done = commit(repo, env, "commit", "-qm", "x")
    assert (done.returncode, done.stdout, done.stderr) == (1, "", TAILNET)


@pytest.mark.usefixtures("repo")
def test_the_origin_is_read_with_git_config_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> tuple[int, str]:
        calls.append(args)
        return 1, ""

    monkeypatch.setattr(pre_commit_scan, "git", fake_git)
    assert pre_commit_scan.visibility() == ""
    assert calls == [("config", "--get", "remote.origin.url")]


def test_the_scan_gets_the_repository_root_from_a_subdirectory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    seen: list[object] = []

    def fake_scan(*_args: object, **kwargs: object) -> None:
        seen.append(kwargs["root"])

    monkeypatch.setattr(secret_scan, "scan", fake_scan)
    assert pre_commit_scan.main([], "tbhbdev alpha pre-commit-scan") == 0
    assert seen == [repo.resolve()]
