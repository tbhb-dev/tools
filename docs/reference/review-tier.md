---
description: Compute and post the review tier for a pull request.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-22T23:58:28Z"
status: stable
tags: [review]
title: Review tier
type: Reference
---

# Review tier

Run `tbhbdev review tier N --config PATH` from a checkout of the repository. Set `TBHBDEV_REVIEW_TIERS` to use a default policy path. The command uses the configured pull-requests App. If the agent config lists more than one entry for this App, select one with `--account`. The installation needs read access to contents and pull requests, and write access to commit statuses.

The highest tier in the diff wins. Prose files in the wiki, drafts, research, and spikes repos start at tier 0. This includes Markdown, reStructuredText, and text files. Files under `designs/` and all other changes start at tier 1. File deletions, incomplete text patches, and edits to the local policy start at tier 2. Use global rules to assign tier 2 to destructive commands, credential handling, workflow paths, rulesets, permissions, harness config, deploy paths, and review or merge code.

The temporary global schema accepts `[tier.0]`, `[tier.1]`, and `[tier.2]` tables. Each table accepts optional `paths` and `patterns` lists. Path globs match whole repository-relative paths, including both sides of a rename. Regular expressions search added and removed lines. See the [example policy](../../tests/testdata/review/review-tiers.toml).

The command reads `.github/review-tiers.local.toml` from the PR base commit. Use the same schema with tier 1 and tier 2 entries. Local rules can raise tiers. The command rejects a lower duplicate of a global rule and any local match below the tier the global policy gives the same file. The command checks the head and base again before writing a success status with the `review-tier` context. The description starts with `tier N:` and includes the first reason at that tier. The command prints every distinct reason at the winning tier. Rule numbers start at one within each policy list.
