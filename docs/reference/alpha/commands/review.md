---
description: Review drafts, threads, policy checks and the run host's review posting.
generated:
  - role: generated
    harness: claude-code:2.1.319
    model: anthropic:claude-sonnet-5-5
    effort: high
    surface: sdk-cli
    session: sha-256-64:e23dce32b8825437
    at: "2026-11-26T03:16:54Z"
status: stable
tags: [alpha]
title: Review commands
type: Reference
---

# Review commands

`review draft N` and `review check N` create and check a review draft.
`review post N [--author LOGIN]` submits a review document as a comment.
`review dismiss N REVIEW_ID --reason TEXT` marks a review as superseded, and `review reply N REVIEW_ID --text TEXT` comments on the pull request.
See [Review drafts](../review-drafts.md) for the formats and checks.

`review threads N [--unresolved]` lists thread IDs, file paths and lines, resolution states, and the first comment's author and first line.
`review resolve N THREAD` and `review unresolve N THREAD` change one thread by its `PRRT_` ID or a review comment's database ID.
They use the current clone's origin and the same GitHub account as `review reply`.

`review policy check [PATH] [--json]` checks the review files of the worktree at PATH, which defaults to the current directory.
It reads the files as data and applies none of them.
A missing file passes, because the floor and its defaults apply.
It checks `.github/pull_request_review.toml` against a closed schema (`config-toml`, `config-schema`).
It checks that the template keeps the floor's required sections in order and gives each section a guidance comment (`template-floor-section`, `template-guidance`).
It checks that the draft schema is valid JSON Schema 2020-12 and sets `additionalProperties` to false (`draft-schema-invalid`, `draft-schema-open`).
It checks that checklist item ids are unique and outside `R1` to `R99`, and that escalation ids are unique and don't start with `FE` (`checklist-frontmatter`, `checklist-item-id`, `checklist-escalate-id`).
An unreadable file is `unreadable`.
The checklist's other frontmatter keys aren't validated yet.
Output is one `PATH: RULE: MESSAGE` line per finding, or with `--json` an object `{"ok": bool, "findings": [{"path", "id", "message"}]}`.
Exit 0 means no findings, 1 means findings, and 2 means PATH isn't a directory or the floor fails to load.
With `--json`, exit 2 still prints one object, `{"ok": false, "error": KIND, "message": TEXT}`, where KIND is `not-a-directory` or `floor-load`.

The run host calls `agent/review_post.py` before finishing a review run whose harness stopped with exit status 0.
The host skips posting if the run name already has a `review` record.
The step reads the run's one `.git/tbhb-alpha/review-N.md` draft, checks it again with the `review check` rules minus the prose linters (a run host lacks them), and requires `commit_id` to equal the commit the run reviewed.
It holds the draft to the floor's schema and sections only, and never reads the repository's policy or schema from the writable worktree.
A draft with a repeated frontmatter key is refused.
The host posts `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` against the pinned commit.
Before an approval it reads the pull request head once and refuses if the head differs.
A request for changes requires non-empty findings.
Any 2xx answer counts as posted.
The review body ends with the run name, harness, model and judged head.
