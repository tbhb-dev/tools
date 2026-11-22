---
description: Vale, cspell and rumdl check of prose before it is posted.
generated:
  - role: generated
    harness: claude
    model: anthropic:claude-sonnet-5-5
    at: "2026-11-22T00:00:00Z"
status: stable
tags: [prose]
title: Prose check
type: Reference
---

# Prose check

`tbhbdev.prose_check` checks Markdown prose before a tool posts it. It's a thin public wrapper over the lint that `tbhbdev alpha pr check` runs, so one path decides what prose may be posted.

```python
check_prose(text: str, what: str, *, root: Path) -> None
```

- `text` is the Markdown to check and `what` names it in the refusal message.
- `root` is the repository root, which holds `.vale.ini`, `.vale/`, `.cspell.jsonc`, `.cspell-words.txt` and `.rumdl.toml`.

The function calls `tbhbdev.alpha.pull_request.lint` with `root` as the working directory, because `lint` finds the repository root from there. It lints the text as `.github/PULL_REQUEST_DRAFT.md`, the path `pr check` uses, so the globs in the three configs match and a vale style missing from `.vale/` is synced first. The working directory is restored afterwards.

It returns `None` when the text is clean. Otherwise it raises `ProseRefused`, a `RuntimeError`, with a message of `what` followed by one `line N: <rule text>` line for each finding, where the rule text is the linter's own message. A missing lint config or a linter that can't run raises it too. `tbhbdev alpha pr check` keeps calling `lint` directly.
