import json
import re
from pathlib import Path
from typing import cast

import pytest
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.tasklists import tasklists_plugin

from tbhbdev.content_model import _metrics, _text_metrics, _visible, normalize


def _parse(source: str) -> SyntaxTreeNode:
    return SyntaxTreeNode(
        MarkdownIt("commonmark", {"inline_definitions": True})
        .enable(["table", "strikethrough"])
        .use(tasklists_plugin)
        .parse(source)
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", (0, 0, 0)),
        ("café 東京 😀", (9, 2, 1)),
        ("one\nline\n\nnext", (14, 3, 2)),
    ],
)
def test_text_metrics(text: str, expected: tuple[int, int, int]) -> None:
    assert tuple(_text_metrics(text).values()) == expected


@pytest.mark.parametrize(
    ("source", "visible"),
    [
        (
            '[label](https://example.test "title") ![alt](image.png) `code`',
            "label alt code",
        ),
        (
            "**bold** *em* ~~gone~~ &amp; <b>tag</b> <!-- hide -->",
            "bold em gone & tag",
        ),
        ("one  \ntwo\nthree", "one\ntwo\nthree"),
        ("```py\nwords\n```\n\n    hidden\n\n<div>hidden</div>\n", ""),
    ],
)
def test_visible(source: str, visible: str) -> None:
    assert _visible(_parse(source)) == visible


def test_structure() -> None:
    source = (
        "# T\n\n## Section\n\nSetext\n------\n\n"
        "- [ ] outer words\n  - [X] child\n  - plain\n\n"
        "1. ordered\n\n> quote\n\n---\n\n```py\na\nb\n```\n\n"
        "    c\n\n<!-- hidden -->\n\n[ref]: https://example.test\n\n"
        "| a | b |\n| - | - |\n| c | d |\n"
    )
    tree = _parse(source)
    counts = _metrics(source, _visible(tree), tree.children)
    expected = {
        "lists": 3,
        "listItems": 4,
        "unorderedLists": 2,
        "orderedLists": 1,
        "taskLists": 2,
        "taskItems": 2,
        "uncheckedItems": 1,
        "thematicBreaks": 1,
        "atxHeadings": 2,
        "setextHeadings": 1,
        "indentedCodeBlocks": 1,
        "fencedCodeBlocks": 1,
        "codeLines": 3,
        "htmlBlocks": 1,
        "linkReferenceDefinitions": 1,
        "paragraphs": 5,
        "tables": 1,
        "blockQuotes": 1,
        "maxBlockDepth": 5,
        "maxListDepth": 2,
    }
    assert {key: counts[key] for key in expected} == expected
    assert counts["blankLines"] == 11
    item = tree.children[3].children[0]
    visible = _visible(item).strip()
    assert visible == "outer words\n\nchild\n\nplain"
    item_metrics = _metrics(source, visible, [item])
    assert cast("dict[str, int]", item_metrics["visible"])["words"] == 4


def _at(value: object, *path: str | int) -> object:
    for key in path:
        value = (
            cast("dict[str, object]", value)[key]
            if isinstance(key, str)
            else cast("list[object]", value)[key]
        )
    return value


def _normalize(source: str) -> dict[str, object]:
    return normalize(source, object="issue", variant="test", metadata={})


@pytest.mark.parametrize(
    ("source", "order"),
    [
        ("# Title", ["title"]),
        ("# T\n## B\n| a\x00b |\n| - |", ["title", "b"]),
        ("# Title\n\n## Out of scope\ntext", ["title", "out-of-scope"]),
        (
            "# Title\n\nintro\n\n## A\n## A\n## A",
            ["title", "preamble", "a", "a-2", "a-3"],
        ),
        (
            "# T\n## Title\n## Preamble\n## !!!",
            ["title", "title-2", "preamble", "section"],
        ),
        (
            "# T\n## *Café* & API\n\nSubsection\n---\n### Detail",
            ["title", "café-api", "subsection"],
        ),
    ],
)
def test_sections(source: str, order: list[str]) -> None:
    result = _normalize(source)
    assert result["blockOrder"] == order
    assert _at(result, "blocks", "title", "heading") in {"Title", "T"}
    assert _at(result, "blocks", "title", "kind") == "heading"
    assert _at(result, "inline") == {"elements": [], "repositoryReferences": []}


@pytest.mark.parametrize(
    "source", ["", "plain", "Title\n===", "## Section", "#", "# T\n##"]
)
def test_required_headings(source: str) -> None:
    with pytest.raises(ValueError, match="heading"):
        _normalize(source)


def test_visible_metrics_and_metadata() -> None:
    source = (
        '# T\n\n## Body\n\n[link](https://example.test "title") '
        "![alt](a.png) `code` &amp; café <!-- hidden -->\n\n"
        "```py\nsecret words\n```\n"
    )
    metadata: dict[str, object] = {"labels": ["one"], "nested": {"flag": True}}
    result = normalize(source, object="pr", variant="test", metadata=metadata)
    assert result["metadata"] is metadata
    assert _at(result, "blocks", "body", "visible") == "link alt code & café"
    assert _at(result, "blocks", "body", "metrics", "visible") == {
        "characters": 20,
        "words": 4,
        "paragraphs": 1,
    }
    assert _at(result, "metrics", "visible") == {
        "characters": 20,
        "words": 4,
        "paragraphs": 1,
    }
    assert _at(result, "metrics", "source", "characters") == len(source)
    assert _at(result, "metrics", "codeLines") == 1
    assert _at(result, "blocks", "body", "content", 1, "info") == "py"


def test_nested_tasks() -> None:
    result = _normalize(
        "# T\n## Work\n- [ ] outer words\n  - [x] child\n  - plain\n\n"
        "3. ordered\n\n   loose paragraph\n"
    )
    path = ("blocks", "work", "content", 0, "items", 0)
    assert _at(result, *path, "checked") is False
    assert _at(result, *path, "metrics", "visible", "words") == 4
    assert _at(result, *path, "blocks", 1, "items", 0, "checked") is True
    assert (
        _at(result, *path, "blocks", 1, "items", 0, "metrics", "visible", "words") == 1
    )
    assert _at(result, "metrics", "maxListDepth") == 2
    assert _at(result, "metrics", "uncheckedItems") == 1
    assert _at(result, "blocks", "work", "content", 1, "start") == 3
    assert _at(result, "blocks", "work", "content", 1, "tight") is False


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_spans(ending: str) -> None:
    source = "# 😀" + ending
    result = _normalize(source)
    assert _at(result, "blocks", "title", "span", "start") == {
        "offset": 0,
        "line": 1,
        "column": 1,
    }
    assert _at(result, "blocks", "title", "span", "end") == {
        "offset": len(source),
        "line": 2 if ending else 1,
        "column": 1 if ending else 4,
    }


def test_all_blocks_and_schema_fields() -> None:
    source = (
        "# T\n\n## Body\n\n> quote\n>\n> second\n\n### ATX\n\nSetext\n===\n\n"
        "---\n\n    code\n\n```py\nfenced\n```\n\n<!-- comment -->\n\n"
        '[ref]: https://example.test "title"\n\n| a | b | c |\n'
        "| :- | :-: | -: |\n| d | e | f |\n\n- item\n\n| x |\n| - |\n"
    )
    result = _normalize(source)
    blocks = cast("dict[str, dict[str, str]]", result["blocks"]).values()
    assert "".join(b["source"] for b in blocks) == source
    assert (
        _at(result, "blocks", "body", "content", 0, "blocks", 1, "visible") == "second"
    )
    assert _at(result, "metrics", "blockQuotes") == 1
    assert _at(result, "blocks", "body", "content", 8, "alignments") == [
        "left",
        "center",
        "right",
    ]
    assert (
        _at(result, "blocks", "body", "content", 8, "rows", 0, "cells", 1, "visible")
        == "e"
    )
    assert _at(result, "blocks", "body", "content", 10, "rows") == []
    cue = Path("schemas/core.cue").read_text()
    _check_fields(result, set(re.findall(r"^\s*(\w+)[!?]?:", cue, re.MULTILINE)))
    assert json.loads(json.dumps(result)) == result


def _check_fields(value: object, fields: set[str]) -> None:
    if isinstance(value, list):
        for child in cast("list[object]", value):
            _check_fields(child, fields)
        return
    if not isinstance(value, dict):
        return
    for key, child in cast("dict[str, object]", value).items():
        assert key in fields
        if key == "metadata":
            continue
        if key == "blocks" and isinstance(child, dict):
            _check_fields(list(cast("dict[str, object]", child).values()), fields)
        else:
            _check_fields(child, fields)


def test_escaped_table_cell() -> None:
    result = _normalize("# T\n## Table\n| a\\|b |\n| - |\n")
    assert (
        _at(result, "blocks", "table", "content", 0, "header", "cells", 0, "source")
        == "a\\|b"
    )
    assert (
        _at(result, "blocks", "table", "content", 0, "header", "cells", 0, "visible")
        == "a|b"
    )


def test_section_heading_is_not_text() -> None:
    result = _normalize("# Title here\n\n## Outcome\n\nOne two three.\n")
    outcome = ("blocks", "outcome")
    assert _at(result, *outcome, "metrics", "visible", "words") == 3
    assert _at(result, *outcome, "metrics", "visible", "characters") == 14
    assert _at(result, *outcome, "metrics", "source", "characters") == 16
    assert _at(result, *outcome, "metrics", "atxHeadings") == 1
    assert str(_at(result, *outcome, "source")).startswith("## Outcome")
    assert _at(result, "metrics", "visible", "words") == 3


def test_issue_comment_has_no_title() -> None:
    result = normalize(
        "Fixed in abc.\n\n## Evidence\n\nrun log\n",
        object="issue-comment",
        variant="work-note",
        metadata={},
    )
    assert result["blockOrder"] == ["preamble", "evidence"]
    assert _at(result, "blocks", "preamble", "kind") == "text"
    assert (
        normalize("", object="issue-comment", variant="x", metadata={})["blockOrder"]
        == []
    )
    with pytest.raises(ValueError, match="heading"):
        normalize("plain", object="issue", variant="x", metadata={})
