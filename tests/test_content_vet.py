from pathlib import Path
from unittest.mock import Mock

import pytest

from tbhbdev import content, content_vet, cue_runtime
from tbhbdev.content_model import normalize

pytestmark = pytest.mark.usefixtures("pinned_cue")  # noqa: V107

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
CI_HINT = "CI already checks this; state the observable outcome instead"
TITLE = "Analysts can download reports"


def issue(markdown: str, variant: str) -> content_vet.Json:
    return normalize(markdown, object="issue", variant=variant, metadata={})


@pytest.fixture
def variant_schema(tmp_path: Path) -> Path:
    variant = tmp_path / "variant.cue"
    variant.write_text(
        """package content
#Fixture: {
    blocks: {
        title: #FeatureTitle
        "acceptance-criteria": #IssueSection & {
            content: [{items: [...#Criterion], ...}]
        }
        verification: #IssueSection & {content: [{items: [...#VerificationItem], ...}]}
        ...
    }
    ...
}
"""
    )
    return variant


class MissingCIRestatementError(AssertionError):
    pass


def miss(criterion: str, reason: str) -> pytest.ParameterSet:
    mark = pytest.mark.xfail(
        strict=True, reason=reason, raises=MissingCIRestatementError
    )
    return pytest.param(criterion, True, marks=mark)


@pytest.mark.parametrize(
    ("criterion", "refused"),
    [
        ("tests pass", True),
        ("CI is green", True),
        ("12/12 pass", True),
        ("tests passed", True),
        ("All tests have passed.", True),
        ("12/12 tests are passing", True),
        miss(
            "12/12 tests have been passing", "Only one numeric auxiliary is accepted."
        ),
        miss("All tests must pass", "The auxiliary list omits the modal must."),
        miss("tests pass!", "The pattern only allows a trailing period."),
        miss("The unit test suite passes", "The pattern allows only one modifier."),
        miss(
            "All unit and integration tests pass", "Coordinated qualifiers are omitted."
        ),
        miss("12/12 checks pass", "The numeric alternative omits the noun checks."),
        ("12/12 passwords hide their characters", False),
        ("The layout shows the name and email", False),
        ("The command reports which tests passed", False),
        ("Users can download a CSV report", False),
        ("CI reports the failing check by name", False),
    ],
)
def test_ci_restatement(criterion: str, refused: bool, variant_schema: Path) -> None:
    for acceptance, verification in (
        (criterion, "Read the report"),
        ("Download the report", criterion),
    ):
        markdown = (
            f"# {TITLE}\n\n## Acceptance criteria\n- [ ] {acceptance}\n\n"
            f"## Verification\n- {verification}\n"
        )
        result = content.check(
            issue(markdown, "Feature"),
            SCHEMAS,
            variant_schema=variant_schema,
            definition="#Fixture",
            parent_type="Epic",
        )
        if refused:
            if not result.refusals:
                raise MissingCIRestatementError(criterion)
            (finding,) = (
                item for item in result.refusals if item.code == "ci-restatement"
            )
            assert content.guide(finding) == CI_HINT
            assert content.format_refusal(finding).endswith(CI_HINT)
        else:
            assert not result.refusals


@pytest.mark.parametrize(
    ("variant", "title", "parent", "refusals", "warnings"),
    [
        ("Epic", TITLE, "Initiative", (), ("epic-title",)),
        ("Feature", "Daily report delivery", "epic", (), ("feature-title",)),
        ("task", "CSV export documentation", None, (), ("task-title",)),
        ("Chore", "Document the export endpoint", None, (), ()),
        ("Defect", "Reports vanish after export", None, (), ("defect-title",)),
        ("Defect", "Reports vanish when filters change", None, (), ()),
        ("Epic", "Daily report delivery", "Task", ("parent-type",), ()),
        ("Feature", TITLE, None, ("parent-type",), ()),
        ("Initiative", "Daily report delivery", None, (), ()),
    ],
)
def test_issue_checks(
    variant: str,
    title: str,
    parent: str | None,
    refusals: tuple[str, ...],
    warnings: tuple[str, ...],
) -> None:
    draft = issue(
        f"# {title}\n\n## Acceptance criteria\nExplain the outcome.\n\n"
        "- [ ] The preview opens and the log updates\n- [x] Show the name and email\n",
        variant,
    )
    result = content.check(draft, SCHEMAS, parent_type=parent)
    assert tuple(item.code for item in result.refusals) == refusals
    assert tuple(item.code for item in result.warnings) == (*warnings, "joined-checks")
    joined_check = result.warnings[-1]
    assert joined_check.path == "blocks.acceptance-criteria.content.1.items.0.visible"


@pytest.mark.parametrize(
    ("kind", "title", "refused"),
    [
        ("Initiative", "Reports reach customers daily", False),
        ("Epic", "Daily report delivery", False),
        ("Feature", TITLE, False),
        ("Task", "Document the export endpoint", False),
        ("Chore", "Update the export dependencies", False),
        ("Defect", "Reports vanish when filters change", False),
        ("Spike", "Can reports arrive by email?", False),
        ("Feature", "Feature enables report delivery", True),
        ("Task", "[Export] Document the endpoint", True),
        ("Defect", "Reports vanish after export.", True),
        ("Spike", "Check whether reports arrive by email", True),
    ],
)
def test_title_definitions(kind: str, title: str, refused: bool) -> None:
    draft = issue(f"# {title}\n", "Fixture")
    definition = f"{{blocks: {{title: #{kind}Title, ...}}, ...}}"
    result = content.check(draft, SCHEMAS, definition=definition)
    assert bool(result.refusals) == refused
    assert all(item.path.startswith("blocks.title.") for item in result.refusals)


def test_vet_reports_multiple_fields() -> None:
    result = content.check({"schemaVersion": 1}, SCHEMAS)
    fields = "blockOrder blocks inline metadata metrics object variant"
    assert {item.path for item in result.refusals} == set(fields.split())
    assert content.format_refusal(result.refusals[0]) == (
        "blockOrder: field is required but not present\n"
        "Revise the value to meet the schema constraint."
    )


def test_vet_reports_invalid_schema(tmp_path: Path) -> None:
    (finding,) = content.check({}, SCHEMAS, definition="#Unknown").refusals
    assert finding == content.Finding("cue", "", 'reference "#Unknown" not found')
    assert "no such file" in content.check({}, tmp_path).refusals[0].message


def test_non_issue() -> None:
    draft = normalize(
        "A comment", object="issue-comment", variant="Feature", metadata={}
    )
    assert content.check(draft, SCHEMAS) == content.CheckResult()


def test_vet_fails_without_a_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cue_runtime, "resolve", lambda: Path("/unused"))
    result = Mock(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(content_vet, "_cue", Mock(return_value=result))
    (finding,) = content.check({}, SCHEMAS).refusals
    assert content.format_refusal(finding).startswith("CUE validation failed.\n")


def test_vet_loads_variant_files_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants = [tmp_path / "sections.cue", tmp_path / "variant.cue"]
    cue = Mock(return_value=Mock(returncode=1, stderr=""))
    monkeypatch.setattr(cue_runtime, "resolve", lambda: tmp_path / "cue")
    monkeypatch.setattr(content_vet, "_cue", cue)
    content_vet.check({}, SCHEMAS, variant_schema=variants, definition="#Variant")
    assert cue.call_args_list[0].args == (
        tmp_path / "cue",
        "vet",
        "--all-errors",
        "-d",
        "#Draft & (#Variant)",
        *(str(SCHEMAS / name) for name in ("core.cue", "taxonomy.cue", "issue.cue")),
        *(str(path) for path in variants),
        "json:",
        "-",
    )


def test_vet_requires_pinned_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "missing"))
    with pytest.raises(cue_runtime.CueError, match="run tbhbdev cue install"):
        content.check({}, SCHEMAS)
