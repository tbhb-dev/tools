---
description: Drafting, checking, posting, and responding to pull request reviews.
generated:
  - role: generated
    harness: codex:0.166.0
    model: openai:gpt-6.2-sol
    surface: exec
    session: sha-256-64:b4a0e82d6519a059
    at: "2026-11-26T15:26:50Z"
status: stable
tags: [alpha]
title: Review drafts
type: Reference
---

# Review drafts

`tbhbdev alpha review draft N` reads the PR's head through `gh` and writes a skeleton to `$(git rev-parse --git-dir)/tbhb-alpha/review-N.md`. It prints the path and keeps an existing draft unchanged. `--force` refreshes only `commit_id`. It keeps the body and the other metadata. Each worktree and PR has its own file.

Set `event` to `APPROVE` for `GO` or `REQUEST_CHANGES` for `BLOCK`. The draft also accepts `COMMENT`. The required frontmatter holds `pr`, which must match N, and the full commit hash in `commit_id`. For a run, use its pinned commit. Fill in the body and remove the guidance comments and any empty optional sections. [Review body format and example](../content.md#review-validation)

`tbhbdev alpha review check N` checks frontmatter and validates the Markdown body in `CUE`. By default it also runs the repository's Markdown, spelling, and prose checks. `--no-lint` skips the local prose checks. Both posting paths run `CUE` and the repository's Vale configuration before sending a review. They refuse error-level alerts other than `Vale.Spelling` and `Vale.Terms`, and refuse to post when Vale or its styles are missing.

## Post a review document

Create `review-N.doc.md` next to `review-N.md`. Put the schema 2 metadata in YAML frontmatter and follow it with the same Markdown sections as a run draft. [Schema 2 metadata](../content.md#review-validation)

The posting command reads `review-N.doc.md` when that file exists. It maps `GO` to `APPROVE` and `NO-GO` to `REQUEST_CHANGES`. The command returns `exists` for an active review with the same decision, run, head, and pull request by the current account or the supplied `--author LOGIN`.

Without a schema 2 document, the command reads `review-N.md` and uses its `APPROVE` or `REQUEST_CHANGES` event. The draft repository must match the origin remote, and the commit must match the current pull request head. The command sends the review through the agent's GitHub account and prints `posted` or `exists` with the review ID when available. Both statuses exit 0. Refusals exit 1 and usage errors exit 2.

## Supersede or reply to a review

`tbhbdev alpha review dismiss N REVIEW_ID --reason TEXT` prefixes the marked comment review's body with `SUPERSEDED:` and the reason. Write the reason on one line in up to 200 characters and omit review and reply markers. A review already superseded or dismissed succeeds without another edit.

`tbhbdev alpha review reply N REVIEW_ID --text TEXT` writes a comment on the pull request with the review ID and the first eight characters of the reviewed commit hash. Omit review and reply markers from the text. On success, the command appends its own marker and prints the new comment ID.

## Review prompts and checklists

The schema 2 prompt renderer uses four templates. Tier 1 uses `tier-1.txt` for the standard review. Tier 2 uses `tier-2-a.txt` to start with the implementation and `tier-2-b.txt` to start with the tests and claimed behavior. Both tier 2 variants read the whole change, require the security lens, and apply the floor's FE escalation rules.

For a re-review, `delta.txt` supplies the previously reviewed head as Base-reviewed. The reviewer judges changes between that head and the new head, records what changed, and keeps unresolved findings by reference. The renderer can also return the schema 2 draft skeleton on its own.

A review run ends its final message with a `VERDICT: GO`, `VERDICT: BLOCK` or `VERDICT: NO-GO` line for the coordinator. Include the full reviewed head after the verdict. The host appends a `Generated-by: LLM` trailer and sets the GitHub review's `commit_id`.

Every prompt includes the correctness (C), security (S), tests and coverage (T), docs (D), and scope (P) checklists. Each question has a stable numbered ID such as C1 or S1. The review floor retains its R and FE IDs.
