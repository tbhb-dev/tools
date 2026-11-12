---
description: Drafting and checking pull request reviews.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: Review drafts
type: Reference
---

# Review drafts

`tbhbdev alpha review draft N` reads the PR's head through `gh` and writes a skeleton to `$(git rev-parse --git-dir)/tbhb-alpha/review-N.md`. It prints the path and keeps an existing draft unchanged. `--force` refreshes only `commit_id`. It keeps the body and the other metadata. Each worktree and PR has its own file.

Pick `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` in the draft's `event` field. The required frontmatter also holds `pr`, which must match N, and a 40-hex `commit_id`. Reviews have no H1 title. Fill Verdict and Verification, then remove the guidance comments and any unused optional sections. Findings holds blocking issues, one list item each with `path:line` and what must change. A request for changes needs findings. Suggestions holds non-blocking improvements.

`tbhbdev alpha review check N` checks the draft locally, including its frontmatter, section order, word limits, prose wrapping, references, trailers, and the repository's Markdown, spelling, and prose rules. It prints each problem as `path:line: problem` on stderr and exits 1 on failure or 0 on success. The check reads local files and runs local linters. Write plain prose, one line per paragraph, cite code as `path:line`, and leave trailers out.
