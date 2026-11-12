"""A native harness commit must preserve every protected Git child."""

import dataclasses
import errno
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import agent_live_git_commit as evidence
import pytest
from agent_host_example import git
from agent_live_evidence import codex_auth, run_default_pytest
from agent_live_git_commit import commit_verdict, git_child_verdict
from test_alpha_agent_git_parent import execute_native_git_probe, real_clone
from test_alpha_agent_sandbox_live import live_only, report

from tbhbdev.alpha import identity, install
from tbhbdev.alpha.agent.decisions.policy import GIT_CONFIG_PATHS
from tbhbdev.alpha.agent.model import Harness, Profile


@pytest.mark.parametrize(
    ("code", "unchanged", "expected"),
    [
        (None, True, "unproven"),
        (0, True, "writable"),
        (1, True, "blocked"),
        (1, False, "changed"),
        (0, False, "changed"),
    ],
)
def test_git_child_verdict_requires_a_refusal_and_host_snapshot(
    code: int | None, unchanged: bool, expected: str
) -> None:
    assert git_child_verdict(code, unchanged=unchanged) == expected


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "same-head",
        "missing-object",
        "no-trailer",
        "wrong-harness",
        "no-model",
        "missing-content",
    ],
)
def test_commit_verdict_requires_the_object_content_and_identity_hook(
    failure: str | None,
) -> None:
    message = (
        "test: proof\n\nGenerated-by: LLM harness=codex:fixture;model=openai:fixture\n"
    )
    message = {
        "no-trailer": "test: proof\n",
        "wrong-harness": message.replace("codex:", "other:"),
        "no-model": message.replace("model=", "missing="),
    }.get(failure or "", message)
    assert commit_verdict(
        "before",
        "before" if failure == "same-head" else "after",
        message,
        present=failure != "missing-object",
        content="" if failure == "missing-content" else "sandbox commit proof\n",
    ) == ("committed" if failure is None else "unproven")


def test_commit_live_test_skips_without_opt_in(tmp_path: Path) -> None:
    result = run_default_pytest(tmp_path, "-q", __file__, "-k", "test_live_")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 skipped" in result.stdout


@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("error", [errno.EPERM, None])
def test_git_probe_records_each_protected_child_and_build_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: Profile, error: int | None
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    for child in GIT_CONFIG_PATHS[1:]:
        path = workspace / child
        if path.name in {"hooks", "info", "modules"}:
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(exist_ok=True)
            path.write_text("original")
    commands: list[list[str]] = []

    def execute(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert kwargs == {
            "cwd": workspace,
            "capture_output": True,
            "check": False,
            "timeout": 30,
        }
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    opened = Path.open

    def denied(
        path: Path,
        mode: str = "r",
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        if path.is_relative_to(workspace / ".git"):
            assert mode == "ab"
            raise OSError(error, "fixture sandbox refusal")
        return opened(path, mode, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(evidence.subprocess, "run", execute)
    monkeypatch.setattr(Path, "open", denied)
    witness = tmp_path / "witness.json"
    evidence.probe(workspace, witness, profile.value)
    expected = dict.fromkeys(GIT_CONFIG_PATHS[1:], error or 1)
    if profile is Profile.BUILD:
        expected |= {"add": 0, "commit": 0}
        assert commands == [
            ["git", "add", "--", evidence.PROOF_FILE],
            ["git", "commit", "-m", "test: sandbox commit proof"],
        ]
        assert (workspace / evidence.PROOF_FILE).read_text() == evidence.PROOF
    else:
        assert commands == []
    assert json.loads(witness.read_text()) == expected


def test_git_child_snapshot_detects_content_modes_and_directory_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "metadata"
    assert evidence.child_snapshot(root) == {}
    root.mkdir()
    child = root / "child"
    child.write_bytes(b"original")
    (root / "alias").symlink_to("child")
    before = evidence.child_snapshot(root)
    assert before["alias"][2] == "child"
    child.write_bytes(b"changed")
    assert evidence.child_snapshot(root) != before
    before = evidence.child_snapshot(root)
    child.chmod(0o700)
    assert evidence.child_snapshot(root) != before
    before = evidence.child_snapshot(root)
    (root / "new").write_bytes(b"new")
    assert evidence.child_snapshot(root) != before


def test_git_child_probe_detects_writable_metadata(tmp_path: Path) -> None:
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    witness = tmp_path / "witness.json"
    evidence.probe(tmp_path, witness, "review")
    observed = json.loads(witness.read_text())
    assert observed == dict.fromkeys(GIT_CONFIG_PATHS[1:], 0)
    assert all(
        (tmp_path / child).read_bytes() == b"\nsandbox write proof\n"
        for child in GIT_CONFIG_PATHS[1:]
    )


@live_only
@pytest.mark.parametrize("profile", Profile)
def test_live_codex_commit_and_git_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: Profile
) -> None:
    account_home = Path.home()
    installed = shutil.which("codex")
    cli = shutil.which("tbhbdev")
    assert installed is not None
    assert cli is not None
    run, paths = real_clone(tmp_path, monkeypatch, Harness.CODEX)
    workspace = Path(paths.worktree)
    name, email = identity.AGENT_IDENT.split(" <", 1)
    for key, value in (
        ("user.name", name),
        ("user.email", email.removesuffix(">")),
        ("commit.gpgsign", "false"),
        ("remote.origin.url", "https://github.com/tbhb-dev/tools"),
    ):
        git(workspace, "config", key, value)
    before_head = git(workspace, "rev-parse", "HEAD")
    search = tuple(
        dict.fromkeys(
            (
                str(Path(installed).parent),
                str(Path(cli).parent),
                str(Path(sys.executable).parent),
                "/usr/bin",
                "/bin",
            )
        )
    )
    sandbox = dataclasses.replace(
        run.settings.sandbox,
        host_path=search,
        path=dataclasses.replace(run.settings.sandbox.path, linux=search, macos=search),
        denied_home_paths=tuple(
            str(account_home / name)
            for name in (".codex", ".ssh", ".claude", ".claude.json")
        ),
    )
    run = dataclasses.replace(
        run,
        request=dataclasses.replace(run.request, profile=profile),
        settings=dataclasses.replace(run.settings, sandbox=sandbox),
    )
    writes = Path(paths.tmpdir if profile is Profile.BUILD else paths.audit_dir)
    writes.mkdir(parents=True, exist_ok=True)
    witness = writes / "git-commit-results.json"
    children = GIT_CONFIG_PATHS[1:]
    before = {child: evidence.child_snapshot(workspace / child) for child in children}
    for name, verb in install.IDENTITY_HOOKS:
        assert (workspace / ".git/hooks" / name).read_text() == install.SHIM.format(
            mark=install.SHIM_MARK, verb=verb, hook=name, origin=install.ORIGIN_GREP
        )
    command = shlex.join(
        (
            sys.executable,
            str(Path(evidence.__file__)),
            str(workspace),
            str(witness),
            profile.value,
        )
    )
    with codex_auth(run, paths, account_home, tmp_path):
        assert execute_native_git_probe(run, paths, command)
    observed = json.loads(witness.read_text())
    verdicts = []
    for child in children:
        status = git_child_verdict(
            observed.get(child),
            unchanged=evidence.child_snapshot(workspace / child) == before[child],
        )
        report(f"codex {profile} {child}: {status}")
        verdicts.append(status)
    assert verdicts == ["blocked"] * len(children)
    after_head = git(workspace, "rev-parse", "HEAD")
    if profile is Profile.BUILD:
        status = commit_verdict(
            before_head,
            after_head,
            git(workspace, "log", "-1", "--format=%B"),
            present=git(workspace, "cat-file", "-t", after_head) == "commit",
            content=git(workspace, "show", f"{after_head}:{evidence.PROOF_FILE}")
            + "\n",
        )
        report(f"codex {profile} git-commit: {status}")
        assert observed["add"] == observed["commit"] == 0
        assert status == "committed"
        assert git(workspace, "rev-parse", "HEAD^") == before_head
        assert (workspace / evidence.PROOF_FILE).read_text() == evidence.PROOF
    else:
        assert after_head == before_head
