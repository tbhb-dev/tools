"""Markdown text and structural metrics for the normalized content model."""

import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from markdown_it.tree import SyntaxTreeNode


def _text_metrics(text: str) -> dict[str, int]:
    return {
        "characters": len(text),
        "words": len(re.findall(r"\b\w+\b", text)),
        "paragraphs": len(re.split(r"\n\s*\n", text.strip())) if text.strip() else 0,
    }


def _visible(node: SyntaxTreeNode) -> str:
    if node.type in {"text", "code_inline"}:
        return node.content
    if node.type in {"softbreak", "hardbreak"}:
        return "\n"
    separators = {
        "root": "\n\n",
        "bullet_list": "\n\n",
        "ordered_list": "\n\n",
        "list_item": "\n\n",
        "blockquote": "\n\n",
        "table": "\n",
        "thead": "\n",
        "tbody": "\n",
        "tr": "\t",
    }
    parts = [_visible(c) for c in node.children]
    if node.type in separators:
        return separators[node.type].join(
            part.strip() for part in parts if part.strip()
        )
    return "".join(parts)


def _checked(node: SyntaxTreeNode) -> bool | None:
    if node.attrs.get("class") != "task-list-item":
        return None
    return 'checked="checked"' in node.children[0].children[0].children[0].content


def _structure(
    node: SyntaxTreeNode, counts: Counter[str], depth: int = 0, list_depth: int = 0
) -> None:
    names = {
        "bullet_list": "unorderedLists",
        "ordered_list": "orderedLists",
        "list_item": "listItems",
        "hr": "thematicBreaks",
        "fence": "fencedCodeBlocks",
        "code_block": "indentedCodeBlocks",
        "html_block": "htmlBlocks",
        "definition": "linkReferenceDefinitions",
        "paragraph": "paragraphs",
        "table": "tables",
        "blockquote": "blockQuotes",
    }
    if node.type in names:
        counts[names[node.type]] += 1
    if node.type in {"bullet_list", "ordered_list"}:
        counts["lists"] += 1
        counts["taskLists"] += node.attrs.get("class") == "contains-task-list"
        list_depth += 1
    if node.type == "heading":
        counts["atxHeadings" if node.markup.startswith("#") else "setextHeadings"] += 1
    if node.type == "list_item":
        checked = _checked(node)
        counts["taskItems"] += checked is not None
        counts["uncheckedItems"] += checked is False
    if node.type in {"fence", "code_block"}:
        counts["codeLines"] += len(node.content.splitlines())
    if node.type in names or node.type == "heading":
        depth += 1
        counts["maxBlockDepth"] = max(counts["maxBlockDepth"], depth)
        counts["maxListDepth"] = max(counts["maxListDepth"], list_depth)
    # Inline tokens and table wrappers are not block nesting levels.
    for child in node.children:
        _structure(child, counts, depth, list_depth)


def _metrics(
    source: str, visible: str, nodes: list[SyntaxTreeNode]
) -> dict[str, object]:
    counts: Counter[str] = Counter(
        dict.fromkeys(
            (
                "lists",
                "listItems",
                "unorderedLists",
                "orderedLists",
                "taskLists",
                "taskItems",
                "uncheckedItems",
                "thematicBreaks",
                "atxHeadings",
                "setextHeadings",
                "indentedCodeBlocks",
                "fencedCodeBlocks",
                "codeLines",
                "htmlBlocks",
                "linkReferenceDefinitions",
                "paragraphs",
                "blankLines",
                "tables",
                "blockQuotes",
                "maxBlockDepth",
                "maxListDepth",
            ),
            0,
        )
    )
    for node in nodes:
        _structure(node, counts)
    counts["blankLines"] = sum(not line.strip() for line in source.splitlines())
    return {
        "source": _text_metrics(source),
        "visible": _text_metrics(visible),
        **counts,
    }
