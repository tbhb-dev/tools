---
description: Pull request review drafts and review tier statuses.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-23T00:53:46Z"
status: stable
tags: [alpha]
title: Review drafts
type: Reference
---

# Review drafts

`tbhbdev alpha review draft N` reads the PR's head through `gh` and writes a skeleton to `$(git rev-parse --git-dir)/tbhb-alpha/review-N.md`. It prints the path and keeps an existing draft unchanged. `--force` refreshes only `commit_id`. It keeps the body and the other metadata. Each worktree and PR has its own file.

Pick `REQUEST_CHANGES` or `COMMENT` in the draft's `event` field. A draft can't hold `APPROVE`: approval comes only from `tbhb-approve`, run by the coordinator, so a reviewer's GO is a `COMMENT`. The required frontmatter also holds `pr`, which must match N, and a 40-hex `commit_id`. Reviews have no H1 title. Fill Verdict and Verification, then remove the guidance comments and any unused optional sections. Findings holds blocking issues, one list item each with `path:line` and what must change. A request for changes needs findings. Suggestions holds non-blocking improvements.

`tbhbdev alpha review check N` checks the draft locally, including its frontmatter, section order, word limits, prose wrapping, references, trailers, and the repository's Markdown, spelling, and prose rules. It prints each problem as `path:line: problem` on stderr and exits 1 on failure or 0 on success. The check reads local files and runs local linters. Write plain prose, one line per paragraph, cite code as `path:line`, and leave trailers out.

## Review tier status

From the repository under review, run `tbhbdev review tier N --config PATH`. Point `PATH` to `.github/review-tiers.toml` from the rules repository. Set `TBHBDEV_REVIEW_TIERS` to pass the same path through the environment.

Prose changes in `wiki`, `drafts`, `research`, and `spikes` start at tier 0. Eligible files have a text patch and an `.adoc`, `.markdown`, `.md`, `.rst`, or `.txt` extension. Changes under `designs/` and other changes start at tier 1. File deletions, incomplete text diffs, and local policy edits require tier 2. Global policy rules cover other tier 2 changes such as destructive commands, credentials, workflows, rulesets, permissions, harness configuration, deployments, reviews, and merges.

The temporary TOML schema accepts `[tier.0]`, `[tier.1]`, and `[tier.2]` tables with `paths` and `patterns` arrays. Globs match whole repository-relative paths, including the previous path of a renamed file. For recursive matches, use `**`. Regular expressions search each added or removed line. See the [fixture](../../../tests/testdata/review/review-tiers.toml) for an example.

Local rules come from `.github/review-tiers.local.toml` at the pull request's base commit. Any local tier 0 rule or match below the file's global tier causes an error. Across the diff, the highest tier takes precedence. With an unchanged head and base, the command prints the tier and its reasons and sets the `review-tier` status to `success`. Its description includes the first reason. App installations need `contents:read`, `pull_requests:read`, and `statuses:write`. If the agent config defines more than one App credential entry, select one with `--account`.
