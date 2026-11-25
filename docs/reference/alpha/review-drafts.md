---
description: Drafting, checking, posting, and responding to pull request reviews.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.2-sol
    at: "2026-11-25T02:25:56Z"
status: stable
tags: [alpha]
title: Review drafts
type: Reference
---

# Review drafts

`tbhbdev alpha review draft N` reads the PR's head through `gh` and writes a skeleton to `$(git rev-parse --git-dir)/tbhb-alpha/review-N.md`. It prints the path and keeps an existing draft unchanged. `--force` refreshes only `commit_id`. It keeps the body and the other metadata. Each worktree and PR has its own file.

Pick `REQUEST_CHANGES` or `COMMENT` in the draft's `event` field. A draft can't hold `APPROVE`: approval comes only from `tbhb-approve`, run by the coordinator, so a reviewer's GO is a `COMMENT`. The required frontmatter also holds `pr`, which must match N, and a 40-hex `commit_id`. Reviews have no H1 title. Fill Verdict and Verification, then remove the guidance comments and any unused optional sections. Findings holds blocking issues, one list item each with `path:line` and what must change. A request for changes needs findings. Suggestions holds non-blocking improvements.

`tbhbdev alpha review check N` checks the draft locally, including its frontmatter, section order, word limits, prose wrapping, references, trailers, and the repository's Markdown, spelling, and prose rules. It prints each problem as `path:line: problem` on stderr and exits 1 on failure or 0 on success. The check reads local files and runs local linters. Write plain prose, one line per paragraph, cite code as `path:line`, and leave trailers out.

## Post a review document

Create `review-N.doc.md` in the same directory as `review-N.md`. Start with YAML frontmatter containing `schema: 2`, `repo: OWNER/REPO`, and `pr: N`. Follow it with the seven lines `Run: NAME`, `Provider: PROVIDER`, `Model: MODEL`, `Variant: a` or `Variant: b`, `Host: HOSTNAME`, `Head: SHA`, and `Tier: 0`, `Tier: 1`, or `Tier: 2`. Use the full lowercase commit SHA and a fully qualified hostname.

After the header, add `## Verdict`, `## Findings`, and `## Verification` in that order. Write `GO` or `NO-GO` in Verdict and describe what you checked in Verification. Each finding starts with `- BLOCK`, `- TECH-DEBT`, or `- SECURITY-DEBT`. Follow the class with `path:line` in backticks and a summary. `NO-GO` requires a `BLOCK` finding, and `GO` disallows one.

`tbhbdev alpha review post N [--author LOGIN]` validates the document and checks the pull request head against the draft. It submits a `COMMENT` review using the agent's own GitHub account. It looks for an active review with the same run, head, and pull request by that account, or by `LOGIN` if supplied. It prints `posted` or `exists` and the review ID when available. Both statuses exit 0. It reports refusals as `path: problem` on stderr and exits 1. Usage errors exit 2.

## Supersede or reply to a review

`tbhbdev alpha review dismiss N REVIEW_ID --reason TEXT` prefixes the marked comment review's body with `SUPERSEDED:` and the reason. The reason is one line of up to 200 characters and must omit review and reply markers. A review already superseded or dismissed succeeds without another edit. The edit is UNVERIFIED against GitHub's API.

`tbhbdev alpha review reply N REVIEW_ID --text TEXT` writes a comment on the pull request with the review ID and the first eight characters of the reviewed commit SHA. Omit review and reply markers from the text. The command appends its own marker and prints the new comment ID.
