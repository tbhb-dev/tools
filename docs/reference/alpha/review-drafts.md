---
description: Drafting, checking, posting, and responding to pull request reviews.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:c860368be54f7f86
    at: "2026-11-26T16:19:50Z"
status: stable
tags: [alpha]
title: Review drafts
type: Reference
---

# Review drafts

`tbhbdev alpha review draft N` reads the PR's head through `gh` and writes a skeleton to `$(git rev-parse --git-dir)/tbhb-alpha/review-N.md`. It prints the path and keeps an existing draft unchanged. `--force` refreshes only `commit_id`. It keeps the body and the other metadata. Each worktree and PR has its own file.

Set `event` to `APPROVE` for GO or `REQUEST_CHANGES` for BLOCK or HOLD. The draft also accepts `COMMENT`. The required frontmatter holds `pr`, which must match N, and a 40-hex `commit_id`. For a review run, set `commit_id` to the run's pinned commit. Reviews have no H1 title. In Summary, give the reasons in one or two sentences. Fill Verification and remove the guidance comments and any unused optional sections. Findings holds blocking issues, one list item each with `path:line` and what must change. A request for changes needs findings. Suggestions holds non-blocking improvements.

`tbhbdev alpha review check N` checks the draft locally, including its frontmatter, section order, word limits, prose wrapping, references, trailers, and the repository's Markdown, spelling, and prose rules. It refuses the old Verdict heading and a copied final verdict line anywhere in the body. Finding classes such as `BLOCK`, `TECH-DEBT`, and `SECURITY-DEBT` can still label findings. The check prints each problem as `path:line: problem` on stderr and exits 1 on failure or 0 on success. Write plain prose, one line per paragraph, cite code as `path:line`, and leave trailers out.

## Post a review document

Create `review-N.doc.md` in the same directory as `review-N.md`. Start with YAML frontmatter containing `schema: 2`, `repo: OWNER/REPO`, `pr: N`, and `verdict: GO` or `verdict: NO-GO`. Follow it with the seven lines `Run: NAME`, `Provider: PROVIDER`, `Model: MODEL`, `Variant: a` or `Variant: b`, `Host: HOSTNAME`, `Head: SHA`, and `Tier: 0`, `Tier: 1`, or `Tier: 2`. Use the full lowercase commit hash and a fully qualified hostname.

Leave a blank line after the header. Add `## Summary`, `## Findings`, and `## Verification` in that order. Give the reasons in Summary and describe what you checked in Verification. Each finding starts with `- BLOCK`, `- TECH-DEBT`, or `- SECURITY-DEBT`. Follow the class with `path:line` in backticks and a summary. `NO-GO` requires a `BLOCK` finding, and `GO` disallows one.

`tbhbdev alpha review post N [--author LOGIN]` reads only `review-N.doc.md`. It validates the document, checks that its repository matches `origin` and that the pull request head matches, and submits a `COMMENT` using the agent's GitHub account. It returns `exists` if that account, or `LOGIN` if supplied, already has an active review for the same run, head, and pull request. A new post requires a recognized agent session and ends with its validated `Generated-by` trailer.

The command prints `posted` or `exists` and the review ID when available. Both statuses exit 0. It reports refusals as `path: problem` on stderr and exits 1. Usage errors exit 2. A schema 2 draft with the old Verdict heading fails with the expected section names. Replace that heading and its decision with a Summary of the reasons, and move the decision to the `verdict` frontmatter field.

## Supersede or reply to a review

`tbhbdev alpha review dismiss N REVIEW_ID --reason TEXT` prefixes the marked comment review's body with `SUPERSEDED:` and the reason. Write the reason on one line in up to 200 characters and omit review and reply markers. A review already superseded or dismissed succeeds without another edit.

`tbhbdev alpha review reply N REVIEW_ID --text TEXT` writes a comment on the pull request with the review ID and the first eight characters of the reviewed commit hash. Omit review and reply markers from the text. On success, the command appends its own marker and prints the new comment ID.

## Review prompts and checklists

The schema 2 prompt renderer uses four templates. Tier 1 uses `tier-1.txt` for the standard review. Tier 2 uses `tier-2-a.txt` to start with the implementation and `tier-2-b.txt` to start with the tests and claimed behavior. Both tier 2 variants read the whole change, require the security lens, and apply the floor's FE escalation rules.

For a re-review, `delta.txt` supplies the previously reviewed head as Base-reviewed. The reviewer judges changes between that head and the new head, records what changed, and keeps unresolved findings by reference. The renderer can also return the schema 2 draft skeleton on its own.

A review run ends its final message with a `VERDICT: GO`, `VERDICT: BLOCK` or `VERDICT: NO-GO` line for the coordinator. The prompt asks for the full reviewed head after the verdict. The host posts only the draft body and run attribution.

Every prompt includes the correctness (C), security (S), tests and coverage (T), docs (D), and scope (P) checklists. Each question has a stable numbered ID such as C1 or S1. The review floor retains its R and FE IDs.
