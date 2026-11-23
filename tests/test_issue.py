from typing import TYPE_CHECKING

import pytest

from tbhbdev import cli, issue_import

if TYPE_CHECKING:
    from pathlib import Path


def test_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "issues.md"
    source.write_text(
        """### out-3/initiative.md
type: Initiative
title: Reports reach every team
criteria:
- People receive their reports
owner: example
repo: demo

### epic
type: Epic
title: Weekly report delivery
criteria:
- Weekly reports arrive on Monday
parent: out-3/initiative.md

### feature
type: Feature
labels: [tech-debt, security-debt, ]
title: Operators can schedule weekly reports
criteria:
- Operators choose a weekday
- Reports arrive that weekday
evidence: First line
Second line
parent: epic
blocked-by: epic, out-3/initiative.md

### task
type: Task
title: Add a schedule picker
verification:
- The picker shows all weekdays
parent: feature
duplicate-of: feature
""",
        encoding="utf-8",
    )
    entries = set(tmp_path.iterdir())
    assert cli.main(["issue", "import", str(source), "--check"]) == 0
    assert capsys.readouterr() == ("Checked 4 issue blocks.\n", "")
    blocks, findings = issue_import.read(source.read_text(encoding="utf-8"))
    assert findings == []
    feature = blocks["feature"]
    assert feature.labels == ("tech-debt", "security-debt")
    assert feature.evidence == "First line\nSecond line"
    assert feature.blocked_by == ("epic", "out-3/initiative.md")
    assert blocks["task"].verification == ("The picker shows all weekdays",)
    assert set(tmp_path.iterdir()) == entries


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", [("<file>", "expected issue blocks starting with ###")]),
        (
            "preamble\n###\n### a\ntype: Chore\n### a\n",
            [
                ("<file>", "expected issue blocks starting with ###"),
                ("<missing id>", "missing or repeated id"),
                ("a", "missing or repeated id"),
            ],
        ),
        ("### a\nlabels: tech-debt", [("a", "labels must be in brackets")]),
        (
            "### a\ncriteria: text",
            [("a", "criteria and verification must use list items")],
        ),
        ("### a\ntype: Task\ntype: Chore", [("a", "repeated field type")]),
        ("### a\nprose", [("a", "expected a field or a list item")]),
    ],
)
def test_read_refusals(source: str, expected: list[tuple[str, str]]) -> None:
    _, findings = issue_import.read(source)
    assert [(entry.id, entry.rule) for entry in findings] == expected


def test_check_refusals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "issues.md"
    source.write_text(
        """### task
type: Task
criteria:
- Unsupported criteria
blocked-by: feature, missing, absent

### epic
type: Epic
parent: feature
duplicate-of: debt

### feature
type: Feature
parent: task
blocked-by: task
criteria:
- One criterion

### debt
type: debt
parent: epic
duplicate-of: absent

### other
type: Incident
title: Reports fail every Monday
parent: absent

### loop
type: Problem
title: All report runs fail
parent: loop
duplicate-of: loop
""",
        encoding="utf-8",
    )
    assert cli.main(["issue", "import", str(source), "--check"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "Refused task: Task must have no criteria (count 1)",
        "Refused task: Task verification must be 1 to 3 (count 0)",
        "Refused task: Task requires a parent (count 0)",
        "Refused epic: criteria must be 1 to 6 (count 0)",
        "Refused epic: Epic requires a parent of type Initiative (count 1)",
        "Refused feature: criteria must be 2 to 5 (count 1)",
        "Refused feature: parent is a Task (count 1)",
        "Refused feature: Feature requires a parent of type Epic (count 1)",
        "Refused debt: unknown type (count 1)",
        "Refused other: unknown parent target (count 1)",
        "Refused loop: parent cycle (count 1)",
        "Refused task: unknown blocked-by target (count 2)",
        "Refused task: blocked-by cycle (count 1)",
        "Refused feature: blocked-by cycle (count 1)",
        "Refused debt: unknown duplicate-of target (count 1)",
        "Refused loop: duplicate-of cycle (count 1)",
        "Warning task: title shape",
        "Warning epic: title shape",
        "Warning feature: title shape",
        "Warning debt: title shape",
    ]


@pytest.mark.parametrize(
    ("title", "warning"),
    [
        ("Bad.", True),
        ("Telecommunications infrastructure", True),
        ("Can people view reports!", True),
        ("[UI] Can people view reports?", True),
        ("Can people view reports", True),
        ("Can people view reports?", False),
    ],
)
def test_title_warning(title: str, warning: bool) -> None:
    blocks, _ = issue_import.read(f"### a\ntype: Spike\ntitle: {title}")
    assert issue_import.title_warning(blocks["a"]) is warning


@pytest.mark.parametrize(
    ("type_", "field", "minimum", "maximum"),
    [
        ("Initiative", "criteria", 1, 6),
        ("Epic", "criteria", 1, 6),
        ("Feature", "criteria", 2, 5),
        ("Defect", "criteria", 1, 3),
        ("Chore", "criteria", 1, 3),
        ("Spike", "criteria", 1, 3),
        ("Task", "verification", 1, 3),
    ],
)
def test_item_count_limits(type_: str, field: str, minimum: int, maximum: int) -> None:
    for size in (minimum - 1, minimum, maximum, maximum + 1):
        source = f"### entry\ntype: {type_}\n{field}:\n" + "- Reports arrive\n" * size
        blocks, _ = issue_import.read(source)
        counts = [
            finding.count
            for finding in issue_import.check(blocks)
            if field in finding.rule
        ]
        assert counts == ([] if minimum <= size <= maximum else [size])


def test_cycle_does_not_refuse_a_predecessor() -> None:
    edges: dict[str, tuple[str, ...]] = {"a": ("b",), "b": ("c",), "c": ("b",)}
    assert not issue_import.in_cycle("a", edges)
    assert issue_import.in_cycle("b", edges)
    assert issue_import.in_cycle("c", edges)


def test_unreadable_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["issue", "import", str(tmp_path / "missing"), "--check"]) == 2
    assert "cannot read issue blocks" in capsys.readouterr().err
