"""Exercise the repository's inline PR draft gate with real Git diffs."""

import subprocess
from pathlib import Path

import pytest
import yaml

from tbhbdev.alpha.agent.open_pr import DRAFT

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


@pytest.mark.parametrize(
    "change", ["add", "modify", "delete", "rename", "other", "bad-base"]
)
def test_pr_draft_gate(tmp_path: Path, change: str) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["pr-draft"]
    assert "pr-draft" in workflow["jobs"]["conclusion"]["needs"]
    assert job["permissions"] == {"contents": "read"}
    checkout, gate = job["steps"]
    assert checkout["if"] == gate["if"] == "github.event_name == 'pull_request'"
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "--initial-branch=main")
    git("config", "user.name", "Example")
    git("config", "user.email", "example@example.com")
    git("config", "commit.gpgsign", "false")
    draft = tmp_path / DRAFT
    draft.parent.mkdir()
    if change in ("modify", "delete"):
        draft.write_text("before\n")
    other = tmp_path / "other.md"
    other.write_text("other\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    if change != "bad-base":
        git("update-ref", "refs/remotes/origin/main", "HEAD")
    if change == "delete":
        draft.rename(tmp_path / "removed.md")
    elif change == "rename":
        other.rename(draft)
    elif change in ("add", "modify"):
        draft.write_text("after\n")
    else:
        other.write_text("changed\n")
    git("add", "-A")
    git("commit", "-qm", "change")
    done = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", gate["run"]],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if change == "other":
        assert done.returncode == 0
        assert done.stdout == ""
    elif change == "bad-base":
        assert done.returncode != 0
    else:
        assert done.returncode == 1
        assert done.stdout == (
            f"PR diff contains {DRAFT}; remove it from the commits\n"
        )
