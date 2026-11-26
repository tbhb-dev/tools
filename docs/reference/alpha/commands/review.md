---
description: Review drafts, threads, policy checks and the run host's review posting.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    surface: exec
    at: "2026-11-26T16:32:40Z"
status: stable
tags: [alpha]
title: Review commands
type: Reference
---

# Review commands

`review draft N` and `review check N` create and check a review draft.
The Summary gives the reasons for the review in one paragraph of at most 250 visible characters. The review event records the verdict.
`review post N [--author LOGIN]` submits an approval or a request for changes.
`review dismiss N REVIEW_ID --reason TEXT` marks a comment review as superseded, and `review reply N REVIEW_ID --text TEXT` comments on the pull request.
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
The host reads the run's one `.git/tbhb-alpha/review-N.md` draft and requires its `commit_id` to equal the pinned commit.
It validates the body with the packaged `CUE` schema and runs Vale with the reviewed repository's configuration.
If Vale's styles are missing on the host, posting fails with a diagnostic.
The host posts `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` against the pinned commit.
Before an approval it reads the pull request head once and refuses if the head differs.
P0 and P1 findings require `REQUEST_CHANGES`.
For headless runs without an output schema, the host reads the final verdict line separately.
Any 2xx response counts as posted.
The review ends with the host's `Generated-by: LLM` trailer, including the run's `variant` when supplied. The review gate reads the commit from GitHub's `commit_id` and the provider from the trailer or the legacy run footer. For legacy reviews, the gate also checks that the footer refers to the same commit.
