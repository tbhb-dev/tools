---
description: Drafting and checking pull request reviews.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-25T02:25:13Z"
status: stable
tags: [alpha]
title: Review drafts
type: Reference
---

# Review drafts

`tbhbdev alpha review draft N` reads the PR's head through `gh` and writes a skeleton to `$(git rev-parse --git-dir)/tbhb-alpha/review-N.md`. It prints the path and keeps an existing draft unchanged. `--force` refreshes only `commit_id`. It keeps the body and the other metadata. Each worktree and PR has its own file.

Pick `REQUEST_CHANGES` or `COMMENT` in the draft's `event` field. A draft can't hold `APPROVE`: approval comes only from `tbhb-approve`, run by the coordinator, so a reviewer's GO is a `COMMENT`. The required frontmatter also holds `pr`, which must match N, and a 40-hex `commit_id`. Reviews have no H1 title. Fill Verdict and Verification, then remove the guidance comments and any unused optional sections. Findings holds blocking issues, one list item each with `path:line` and what must change. A request for changes needs findings. Suggestions holds non-blocking improvements.

`tbhbdev alpha review check N` checks the draft locally, including its frontmatter, section order, word limits, prose wrapping, references, trailers, and the repository's Markdown, spelling, and prose rules. It prints each problem as `path:line: problem` on stderr and exits 1 on failure or 0 on success. The check reads local files and runs local linters. Write plain prose, one line per paragraph, cite code as `path:line`, and leave trailers out.

## Review prompts and checklists

The schema 2 prompt renderer uses four templates. Tier 1 uses `tier-1.txt` for the standard review. Tier 2 uses `tier-2-a.txt` to start with the implementation and `tier-2-b.txt` to start with the tests and claimed behavior. Both tier 2 variants read the whole change, require the security lens, and apply the floor's FE escalation rules.

For a re-review, `delta.txt` supplies the previously reviewed head as Base-reviewed. The reviewer judges changes between that head and the new head, records what changed, and keeps unresolved findings by reference. The renderer can also return the schema 2 draft skeleton on its own.

Every prompt includes the correctness (C), security (S), tests and coverage (T), docs (D), and scope (P) checklists. Each question has a stable numbered ID such as C1 or S1. The review floor retains its R and FE IDs.
