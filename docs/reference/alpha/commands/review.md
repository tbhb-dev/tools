---
description: Review verification, drafts, threads, policy checks and the run host's review posting.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:f74a623ec82c05d1
    at: "2026-11-26T16:04:56Z"
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

## Verify reviews

`tbhbdev alpha review verify N [--repo OWNER/NAME]` reads GitHub with the agent's `gh` account and evaluates the pull request at its current head. The repository defaults to the clone's origin. The pull request must be open, ready for review, and authored by an account other than `tbhb` or the reviews App.

At least one check besides `reviews-verified` must have a run at the head. The latest run of each such check must finish as success, skipped, or neutral. In private repositories, each Actions job attempt at the head with a conclusion other than skipped must have a runner group whose name starts with `tbhb-dev-`.

Tier 2 applies when any changed path matches a glob in `.github/review-tier-2-paths` at the base commit. The file holds one repository-relative glob per line, and renames count at both paths. All other pull requests use tier 1.

The reviews App must approve the exact head, and any request for changes from the App at that head fails verification. Each run's latest review counts. Approvals must come from providers outside the set identified by the commits' `Generated-by` trailers. Tier 1 needs one approval when every commit identifies a known builder. If any commit lacks a `Generated-by` trailer identifying a known builder, both tiers require approvals from two different providers. A trailer with an unknown harness fails verification. Tier 2 also requires approvals in two distinct variants. An older review stamp without a variant can count for tier 1.

A pass prints `reviews-verified: pass <sha>` and exits 0. A failure prints one line per unmet condition and exits 1.

## Reviews posted by the run host

The run host calls `agent/review_post.py` before finishing a review run whose harness stopped with exit status 0.
The host skips posting if the run name already has a `review` record.
The step reads the run's one `.git/tbhb-alpha/review-N.md` draft, checks it again with the `review check` rules minus the prose linters (a run host lacks them), and requires `commit_id` to equal the commit the run reviewed.
It holds the draft to the floor's schema and sections only, and never reads the repository's policy or schema from the writable worktree.
A draft with a repeated frontmatter key is refused.
The host posts `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` against the pinned commit.
Before an approval it reads the pull request head once and refuses if the head differs.
A request for changes requires non-empty findings.
Any 2xx answer counts as posted.
The review body ends with the run name, harness, model, judged head and the variant when the run specifies one.
