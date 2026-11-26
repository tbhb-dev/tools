---
description: Per-commit attribution and signature checks over a range.
status: stable
tags: [alpha]
title: Verify range
type: Reference
---

# Verify range

`tbhbdev alpha verify-range BASE TIP [-C PATH]` checks each commit in `BASE..TIP`, oldest first.
A commit passes when the agent is its author and committer, it has a signature header and valid attribution trailers, and it has exactly one parent.
A merge commit passes without those checks, and the commits it brings in are checked like any other.
A root commit fails.
Sign-off trailers, co-author trailers and GitHub's no-reply addresses fail it.
Exit codes are 0 when every commit passes, 1 when any fails and 2 for usage or Git errors.
