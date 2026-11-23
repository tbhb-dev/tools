"""Normalize Markdown into the Draft vocabulary in schemas/core.cue."""

import re
from bisect import bisect_right
from collections import Counter
from typing import cast
lazy import builtins

from markdown_it import MarkdownIt
from markdown_it.rules_block.html_block import HTML_SEQUENCES
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.tasklists import tasklists_plugin


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


class _Document:
    def __init__(self, source: str) -> None:
        self.source = source
        self.offsets = [0, *(m.end() for m in re.finditer(r"\r\n?|\n", source))]
        if self.offsets[-1] != len(source):
            self.offsets.append(len(source))

    def point(self, offset: int) -> dict[str, int]:
        line = bisect_right(self.offsets, offset) - 1
        if offset == len(self.source) and not self.source.endswith(("\n", "\r")):
            line -= 1
        return {
            "offset": offset,
            "line": line + 1,
            "column": offset - self.offsets[line] + 1,
        }

    def value(
        self, nodes: list[SyntaxTreeNode], start: int, end: int
    ) -> dict[str, object]:
        source = self.source[start:end]
        visible = "\n\n".join(text for n in nodes if (text := _visible(n).strip()))
        return {
            "format": "markdown",
            "source": source,
            "visible": visible,
            "span": {"start": self.point(start), "end": self.point(end)},
            "metrics": _metrics(source, visible, nodes),
        }

    def block(self, node: SyntaxTreeNode) -> dict[str, object]:
        start, end = cast("tuple[int, int]", node.map)
        result = self.value([node], self.offsets[start], self.offsets[end])
        kind = {
            "bullet_list": "list",
            "ordered_list": "list",
            "list_item": "list-item",
            "fence": "code-block",
            "code_block": "code-block",
            "html_block": "html-block",
            "blockquote": "block-quote",
            "hr": "thematic-break",
            "definition": "link-reference-definition",
        }.get(node.type, node.type)
        result["kind"] = kind
        if kind in {"paragraph", "heading"}:
            result["inlines"] = list[builtins.object]()
        result.update(self.details(node, kind))
        if kind in {"list", "list-item", "block-quote"}:
            result["items" if kind == "list" else "blocks"] = [
                self.block(c) for c in node.children
            ]
        if kind == "list-item":
            checked = _checked(node)
            if checked is not None:
                result["checked"] = checked
        if kind == "list":
            result.update(
                style="ordered" if node.type == "ordered_list" else "unordered",
                tight=all(c.children[0].hidden for c in node.children if c.children),
            )
            if node.type == "ordered_list":
                result["start"] = node.attrs.get("start", 1)
        return result

    def details(self, node: SyntaxTreeNode, kind: str) -> dict[str, object]:
        result: dict[str, object] = {}
        if kind == "heading":
            result.update(
                style="atx" if node.markup.startswith("#") else "setext",
                level=int(node.tag[1]),
            )
        if kind == "code-block":
            result["style"] = "fenced" if node.type == "fence" else "indented"
            if node.type == "fence":
                result["info"] = node.info
        if kind == "html-block":
            result["htmlType"] = next(
                i
                for i, (pattern, _, _) in enumerate(HTML_SEQUENCES, 1)
                if pattern.search(node.content.lstrip().split("\n", 1)[0])
            )
        if kind == "link-reference-definition":
            meta = cast("dict[str, str]", node.meta)
            result.update(
                label=meta["label"], destination=meta["url"], title=meta["title"]
            )
        if kind == "table":
            result.update(self.table(node))
        return result

    def table(self, node: SyntaxTreeNode) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        alignments: list[str] = []
        for row in (r for group in node.children for r in group.children):
            cells: list[dict[str, object]] = []
            line, _ = cast("tuple[int, int]", row.map)
            cursor = self.offsets[line]
            for cell in row.children:
                inline = cell.children[0]
                # Inline content identifies the cell without its table separators.
                pattern = re.escape(inline.content).replace(r"\|", r"\\?\|")
                match = cast(
                    "re.Match[str]",
                    re.compile(pattern).search(
                        self.source, cursor, self.offsets[line + 1]
                    ),
                )
                start = match.start()
                value = self.value([inline], start, match.end())
                value.update(kind="table-cell", inlines=[])
                cells.append(value)
                cursor = match.end()
                if row.tag == "tr" and not rows:
                    alignments.append(
                        str(cell.attrs.get("style", "none")).removeprefix("text-align:")
                    )
            rows.append({"cells": cells})
        return {
            "header": rows[0],
            "rows": rows[1:],
            "alignments": alignments,
            "columnCount": len(alignments),
        }


def _groups(nodes: list[SyntaxTreeNode]) -> list[list[SyntaxTreeNode]]:
    groups: list[list[SyntaxTreeNode]] = []
    for node in nodes:
        if (node.type == "heading" and node.tag == "h2") or not groups:
            groups.append([])
        groups[-1].append(node)
    return groups


def _validate_title(nodes: list[SyntaxTreeNode]) -> None:
    if (
        not nodes
        or nodes[0].type != "heading"
        or nodes[0].markup != "#"
        or not _visible(nodes[0]).strip()
    ):
        message = "Markdown must start with a nonempty level-one ATX heading"
        raise ValueError(message)


def _without_heading(
    block: dict[str, object], heading: str, body: list[SyntaxTreeNode]
) -> None:
    """Make the block's word, character and paragraph metrics describe the body."""
    visible = "\n\n".join(text for n in body if (text := _visible(n).strip()))
    metrics = cast("dict[str, object]", block["metrics"])
    metrics["visible"] = _text_metrics(visible)
    source = cast("str", block["source"])
    metrics["source"] = _text_metrics(source.removeprefix(heading))
    block["visible"] = visible


def _group_block(
    doc: _Document, group: list[SyntaxTreeNode], start: int, end: int
) -> tuple[str, dict[str, object]]:
    """Return the key and block for a preamble or a level-two section."""
    first = group[0]
    section = first.type == "heading" and first.tag == "h2"
    block = doc.value(group, start, end)
    block.update(
        kind="section" if section else "text",
        inline={"elements": [], "repositoryReferences": []},
        content=[doc.block(n) for n in group[int(section) :]],
    )
    if not section:
        return "preamble", block
    heading = _visible(first).strip()
    if not heading:
        message = "Section headings must have visible text"
        raise ValueError(message)
    block.update(heading=heading, level=2)
    heading_end = cast("tuple[int, int]", first.map)[1]
    _without_heading(block, doc.source[start : doc.offsets[heading_end]], group[1:])
    return re.sub(r"[\W_]+", "-", heading.lower()).strip("-") or "section", block


def normalize(
    markdown: str,
    *,
    object: str,  # noqa: A002 -- Required API.
    variant: str,
    metadata: dict[str, builtins.object],
) -> dict[str, builtins.object]:
    """Return a core.cue Draft.

    Input starts with a nonempty level-one ATX title, except for the
    `issue-comment` and `pr-review` objects, which keep leading text as a
    `preamble` block. Section and draft word, character and paragraph metrics
    exclude headings. Metadata must already contain JSON values. Frontmatter is
    the caller's job.
    """
    titled = object not in {"issue-comment", "pr-review"}
    markdown = markdown.replace("\x00", "\ufffd")  # The parser does too.
    parser = (
        MarkdownIt("commonmark", {"inline_definitions": True})
        .enable(["table", "strikethrough"])
        .use(tasklists_plugin)
    )
    nodes = SyntaxTreeNode(parser.parse(markdown)).children
    doc = _Document(markdown)
    blocks: dict[str, dict[str, builtins.object]] = {}
    groups = _groups(nodes[1:] if titled else nodes)
    starts = [doc.offsets[cast("tuple[int, int]", g[0].map)[0]] for g in groups]
    if titled:
        _validate_title(nodes)
        title = doc.value(nodes[:1], 0, starts[0] if starts else len(markdown))
        title.update(
            kind="heading",
            heading=_visible(nodes[0]).strip(),
            level=1,
            inline={"elements": [], "repositoryReferences": []},
            content=[],
        )
        blocks["title"] = title
    ends = [*starts[1:], len(markdown)][: len(starts)]
    for group, start, end in zip(groups, starts, ends, strict=True):
        key, block = _group_block(doc, group, start, end)
        candidate, suffix = key, 1
        while candidate in blocks:
            suffix += 1
            candidate = f"{key}-{suffix}"
        blocks[candidate] = block
    visible = "\n\n".join(
        text
        for n in nodes
        if not (n.type == "heading" and n.tag in {"h1", "h2"})
        and (text := _visible(n).strip())
    )
    return {
        "schemaVersion": 1,
        "object": object,
        "variant": variant,
        "metadata": metadata,
        "blockOrder": list(blocks),
        "blocks": blocks,
        "inline": {"elements": [], "repositoryReferences": []},
        "metrics": _metrics(markdown, visible, nodes),
    }
