import pytest

from tbhbdev.content import check_decision_record

RECORD = """\
# Use a shared decision log

## Context

Teams need a place to find decisions.

## Decision

Keep decisions in a shared log.

## Consequences

Teams can link to decisions directly.
"""


def _words(count: int) -> str:
    return " ".join(["word"] * count)


def _options(content: str) -> str:
    return RECORD.replace(
        "## Consequences", f"## Options considered\n\n{content}\n\n## Consequences"
    )


def _record_with_counts(context: int, decision: int, consequences: int) -> str:
    return (
        "# Use shared decision logs\n\n## Context\n\n"
        f"{_words(context)}\n\n## Decision\n\n{_words(decision)}"
        f"\n\n## Consequences\n\n{_words(consequences)}"
    )


def test_decision_record_accepts_visible_words_and_single_line_paragraphs() -> None:
    markdown = (
        RECORD.replace("Use a shared decision log", "Use a [shared log][log]")
        .replace(
            "Keep decisions in a shared log.",
            '"Decision log" names the shared file.\n\nUse `records`.',
        )
        .replace("## Context", "Context\n-------")
        + '\n[log]: https://example.test/log "Shared log"\n'
    )
    assert check_decision_record(markdown) == []


def test_decision_record_accepts_word_limits() -> None:
    markdown = _record_with_counts(150, 120, 150).replace(
        "## Consequences",
        f"## Options considered\n\n- {_words(25)}\n- two\n- three\n"
        "- four\n- last option\n\n## Consequences",
    )
    assert check_decision_record(markdown) == []
    assert check_decision_record(
        markdown.replace("- last option", "- last option remains")
    ) == ["Record body exceeds 450 words"]


@pytest.mark.parametrize(
    ("counts", "refusal"),
    [
        ((151, 120, 150), "Context exceeds 150 words"),
        ((150, 121, 150), "Decision exceeds 120 words"),
        ((150, 120, 151), "Consequences exceeds 150 words"),
    ],
)
def test_decision_record_section_limits(
    counts: tuple[int, int, int], refusal: str
) -> None:
    assert check_decision_record(_record_with_counts(*counts)) == [refusal]


@pytest.mark.parametrize(
    ("markdown", "refusal"),
    [
        ("", "Markdown must start with a nonempty level-one ATX heading"),
        (
            "## Context\n\n" + RECORD,
            "Markdown must start with a nonempty level-one ATX heading",
        ),
        (RECORD.replace("## Context", "##"), "Section headings must have visible text"),
        (
            RECORD.replace("Use a shared decision log", "Use shared logs"),
            "Title needs at least 4 words",
        ),
        (
            RECORD.replace("decision log", "decision log:", 1),
            "End the title without a colon or question mark",
        ),
        (
            RECORD.replace("decision log", "**decision log?**", 1),
            "End the title without a colon or question mark",
        ),
        (
            RECORD.replace("Use a shared decision log", '"Use a shared decision log"'),
            "Start the title without a quoted sentence",
        ),
    ],
)
def test_decision_record_title_and_normalization(markdown: str, refusal: str) -> None:
    assert check_decision_record(markdown) == [refusal]


@pytest.mark.parametrize(
    "markdown",
    [
        RECORD.replace("## Context\n\nTeams need a place to find decisions.\n\n", ""),
        RECORD.replace("## Decision", "## Context").replace(
            "## Context", "## Decision", 1
        ),
        RECORD.replace("## Consequences", "## Consequence"),
        RECORD.replace("## Context", "## context"),
        RECORD + "\n## Context\n",
        _options("- Keep a log.").replace("## Options considered", "## Extra"),
        RECORD.replace(
            "## Decision", "## Options considered\n\n- Keep a log.\n\n## Decision"
        ),
        RECORD.replace("## Context", "Intro text\n\n## Context"),
    ],
)
def test_decision_record_requires_section_order(markdown: str) -> None:
    assert check_decision_record(markdown) == [
        (
            "Use these H2 sections in order: Context, Decision, "
            "optional Options considered, Consequences"
        )
    ]


@pytest.mark.parametrize(
    ("markdown", "refusals"),
    [
        (
            RECORD + "\n### Details\n",
            ["Use headings only for the H1 title and H2 sections"],
        ),
        (
            RECORD + "\n# Add another main heading\n",
            ["Use headings only for the H1 title and H2 sections"],
        ),
        (
            RECORD + "\n> ## Quoted heading\n",
            [
                "Use headings only for the H1 title and H2 sections",
                "Remove block quotes",
            ],
        ),
        (RECORD + "\n| A | B |\n| - | - |\n", ["Remove tables"]),
        (RECORD + "\n```\n```\n", ["Remove code blocks"]),
        (RECORD + "\n    code\n", ["Remove code blocks"]),
        (RECORD + "\n> Quoted prose.\n", ["Remove block quotes"]),
    ],
)
def test_decision_record_rejects_blocks(markdown: str, refusals: list[str]) -> None:
    assert check_decision_record(markdown) == refusals


@pytest.mark.parametrize(
    ("content", "refusals"),
    [
        ("", []),
        ("- Keep a log.\n\n* Keep a wiki.", []),
        ("Consider a wiki.", ["Options considered must use flat bullet lists"]),
        ("1. Consider a wiki.", ["Options considered must use flat bullet lists"]),
        (
            "- Keep a log.\n  - Link the log.",
            ["Options considered must use flat bullet lists"],
        ),
        ("- Keep a log.\n" * 6, ["Options considered exceeds 5 bullets"]),
        (
            f"- Short option.\n\n* {_words(26)}",
            ["Option 2 exceeds 25 words"],
        ),
        (
            '- **"Use a wiki."** — a suggestion\n- Keep a log\n  with links.',
            [
                "Line 13: start the line without a quoted sentence",
                "Line 14: write each paragraph on one source line",
            ],
        ),
    ],
)
def test_decision_record_options(content: str, refusals: list[str]) -> None:
    assert check_decision_record(_options(content)) == refusals


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_decision_record_rejects_wrapping(newline: str) -> None:
    markdown = RECORD.replace(
        "Teams need a place to find decisions.", 'Teams need a log.  \n"Use a log."'
    )
    assert check_decision_record(markdown.replace("\n", newline)) == [
        "Line 5: write each paragraph on one source line",
        "Line 5: start the line without a quoted sentence",
    ]


@pytest.mark.parametrize("last_line", ["X", "\u00a0"])
def test_decision_record_checks_the_source_line(last_line: str) -> None:
    markdown = RECORD.replace(
        "Teams need a place to find decisions.", f"Teams need a log.\n{last_line}"
    )
    assert check_decision_record(markdown) == [
        "Line 5: write each paragraph on one source line"
    ]


@pytest.mark.parametrize(
    "text",
    [
        '"Build a log," the team said.',
        "“Build a log.”",
        "'Build a log.'",
        "&#8216;Build a log.&#8217;",
    ],
)
def test_decision_record_rejects_quoted_sentences(text: str) -> None:
    assert check_decision_record(
        RECORD.replace("Teams need a place to find decisions.", text)
    ) == ["Line 5: start the line without a quoted sentence"]
