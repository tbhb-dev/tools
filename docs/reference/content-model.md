---
description: Markdown normalization into the content schema.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.2-sol
    at: "2026-11-23T02:43:02Z"
status: stable
tags: [plugins]
title: Content model
type: Reference
---

# Content model

`normalize(markdown, *, object, variant, metadata)` in `tbhbdev.content_model` returns a JSON-compatible dictionary using `#Draft` from `schemas/core.cue`.

Supply a schema-supported object, a variant name, and JSON-compatible metadata. Metadata passes through unchanged. The caller parses frontmatter.

Start the document with a nonempty level-one ATX (hash-prefixed) heading for the `title` block. Issue comments and pull request reviews are title-free. Level-two headings start sections. Section keys use visible heading text, lowercased, with runs of non-alphanumeric characters replaced by a hyphen. Duplicate keys receive suffixes starting at `-2`. Content before the first section becomes `preamble` when present. This block has kind `text` and omits optional heading and level fields. Missing required titles and empty section headings raise `ValueError`.

`blockOrder` preserves document order. Blocks include source, visible text, spans, metrics, content, and an empty inline index. Sections include their heading in source. Their visible text and metrics exclude it. Their content arrays contain the following blocks. The title has an empty content array.

Offsets count scalars from zero. Lines and columns start at one. Span ends are exclusive. Child source ranges include original line markers and indentation. Spans cover text, so blank lines between top-level blocks belong to no span. A NUL character in the input becomes U+FFFD, as in the parser.

The parser enables tables and strikethrough in `markdown-it-py` and uses task lists from `mdit-py-plugins`. It emits paragraphs, headings, nested lists, fenced and indented code, tables, block quotes, thematic breaks, HTML blocks, and link reference definitions. Task items include `checked` and item metrics.

Visible text counts link labels, image alt text, and inline code. The parser decodes entities and omits destinations, titles, formatting markers, HTML tags and comments, and block code. Nonempty blocks join with blank lines. Table rows use newlines and cells use tabs.

Text metrics count scalar characters, words using Python Unicode boundaries (`\b\w+\b`), and blank-line-separated paragraphs. Source metrics include Markdown and code. Structural metrics count parsed blocks, list and task status, code styles and lines, tables, quotes, headings, reference definitions, and blank source lines. Blank lines have no separate content entries.

`maxBlockDepth` counts nested blocks, including list items, starting at one. Inline elements and table wrappers add no depth. `maxListDepth` counts nested list containers starting at one. Child metrics restart depth counts at that child.

Tech debt: inline indexes and content `inlines` arrays are empty pending inline normalization. Word counting follows Python regular expressions. The task-list plugin can keep checkbox label text when a link reference definition uses the same label. Hostile-input limits and validation of caller-supplied object names and metadata remain with the caller.

Security debt: none introduced by this in-memory transformation.
