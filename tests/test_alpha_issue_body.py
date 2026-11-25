"""Checks for rendered issue bodies and checkout-provided schemas."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tbhbdev import content_vet, cue_runtime
from tbhbdev.alpha import issue_body

pytestmark = pytest.mark.usefixtures("pinned_cue")  # noqa: V107

PM_DIR = Path(os.environ.get("TBHB_PM_DIR") or "/nonexistent")
BODY = "# Reports vanish when filtered\n\n## Outcome\n\nKeep reports visible.\n"


def test_issue_body_passes_bandit() -> None:
    pytest.importorskip("bandit")
    source = Path(__file__).resolve().parents[1] / "src/tbhbdev/alpha/issue_body.py"
    result = subprocess.run(
        [sys.executable, "-m", "bandit", "-q", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout or result.stderr


@pytest.fixture
def pm_dir(tmp_path: Path) -> Path:
    (tmp_path / "templates").mkdir()
    (tmp_path / "schemas").mkdir()
    (tmp_path / "templates/defect.md").write_text("# {{title}}\n\n## Outcome\n")
    (tmp_path / "schemas/issue-sections.cue").write_text(
        'package content\n#Outcome: #IssueSection & {heading: "Outcome"}\n'
    )
    (tmp_path / "schemas/issue.defect.cue").write_text(
        """package content
        #Variant: {
            metadata: {
                type: "Defect"
                area: #AreaLabel
                fields: {"work-type": #WorkType, severity: #Severity}
                ...
            }
            blocks: {title: #DefectTitle, outcome: #Outcome, ...}
            ...
        }
        """
    )
    return tmp_path


@pytest.mark.parametrize(
    "body",
    [
        "# Title\n",
        "Preamble\n",
        "```md\n## Fenced\n```",
        "Setext\n------",
        "### Subheading",
    ],
)
def test_requires_atx_section(body: str) -> None:
    assert "## " in issue_body.check(body, "Defect", None)[0]


def test_requires_title_for_schema(pm_dir: Path) -> None:
    assert (
        "level-one ATX heading" in issue_body.check("## Outcome", "Defect", pm_dir)[0]
    )


def test_unknown_type(pm_dir: Path) -> None:
    assert issue_body.check(BODY, "Other", pm_dir) == ["Unknown issue type: Other"]


@pytest.mark.parametrize(
    "file",
    ["templates/defect.md", "schemas/issue.defect.cue", "schemas/issue-sections.cue"],
)
def test_requires_pm_files(pm_dir: Path, file: str) -> None:
    (pm_dir / file).unlink()
    assert issue_body.check(BODY, "Defect", pm_dir) == [f"Missing {pm_dir / file}"]


def test_valid_body_ignores_incomplete_metadata(pm_dir: Path) -> None:
    assert issue_body.check(BODY, "defect", pm_dir) == []


def test_bad_block_is_refused(pm_dir: Path) -> None:
    refusals = issue_body.check(
        BODY.replace("## Outcome", "## OutCome"), "Defect", pm_dir
    )
    assert len(refusals) == 1
    assert refusals[0].startswith("blocks.outcome.heading: conflicting values")


def test_keeps_concrete_metadata_error(pm_dir: Path) -> None:
    variant = pm_dir / "schemas/issue.defect.cue"
    variant.write_text(variant.read_text().replace('type: "Defect"', 'type: "Task"'))
    (refusal,) = issue_body.check(BODY, "Defect", pm_dir)
    assert refusal.startswith("metadata.type: conflicting values")


def test_keeps_block_error_alongside_incomplete_metadata(
    pm_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incomplete = content_vet.Finding("cue", "metadata.area", "incomplete value")
    block = content_vet.Finding("cue", "blocks.outcome", "Invalid outcome")
    warning = content_vet.Finding("defect-title", "blocks.title", "Review the title")
    result = content_vet.CheckResult((incomplete, block), (warning,))
    monkeypatch.setattr(content_vet, "check", Mock(return_value=result))
    assert issue_body.check(BODY, "Defect", pm_dir) == [
        content_vet.format_refusal(block)
    ]


def test_cue_unavailable(pm_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        content_vet, "check", Mock(side_effect=cue_runtime.CueError("cue missing"))
    )
    assert issue_body.check(BODY, "Defect", pm_dir) == ["cue missing"]


@pytest.mark.skipif(not PM_DIR.is_dir(), reason="set TBHB_PM_DIR to a pm checkout")
def test_pm_examples() -> None:
    testdata = PM_DIR / "schemas/testdata"
    examples = sorted(
        [*testdata.glob("*/*.valid.md"), *testdata.glob("*/*.invalid.md")]
    )
    if not examples:
        pytest.skip("pm checkout has no issue schema examples")
    for path in examples:
        refusals = issue_body.check(
            path.read_text(encoding="utf-8"), path.parent.name, PM_DIR
        )
        assert bool(refusals) == path.name.endswith(".invalid.md"), (path, refusals)
