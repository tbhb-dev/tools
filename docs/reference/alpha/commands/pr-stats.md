---
description: One JSON row per merged or closed pull request, with cycle time.
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
title: Pull request stats
type: Reference
---

# Pull request stats

`tbhbdev alpha pr-stats OWNER/REPO [--limit N]` prints one JSON row for each merged or closed pull request.
The cycle time starts at the earliest of every `started_at` on its identity lines and its earliest commit's committer date, and ends at `mergedAt` or, for a pull request closed without merging, `closedAt`.
A row's `start_source` is `started_at` or `commit`, and its `state` is `merged` or `closed`.
