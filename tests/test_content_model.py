from typing import cast

import pytest
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.tasklists import tasklists_plugin

from tbhbdev.content_model import _metrics, _text_metrics, _visible


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
