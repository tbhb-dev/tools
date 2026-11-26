---
description: The hook that blocks raw gh writes and names the tbhbdev alpha command to run instead.
status: stable
tags: [alpha]
title: Guard gh pr
type: Reference
---

# Guard gh pr

`guard-gh-pr` is a PreToolUse hook that blocks `gh pr create`, `gh pr edit`, `gh pr merge`, `gh pr ready`, `gh pr close`, `gh issue close`, `gh run rerun` and a `gh api` DELETE of a branch ref, starts its denial with `tbhbdev hint:`, names the `tbhbdev alpha` command to run instead, and ends with a pointer to `tbhbdev alpha --help`.
