---
description: Index of the interim agent command, hook and installation pages.
status: stable
tags: [alpha]
title: Alpha commands
type: Reference
---

# Alpha commands

`tbhbdev alpha` holds the interim agent tools that the tbhb-dev repositories used to carry in `tools/interim`.
Each command or hook has its own page under `docs/reference/alpha/commands/`.

- [Pre-push guard](commands/pre-push-guard.md) - Refusals for pushes to main and history rewrites, and signature requirements.
- [Identity](commands/identity.md) - Agent and model attribution, the Generated-by trailers, and the identity start time.
- [Guard op](commands/guard-op.md) - The 1Password account flag requirement on shell calls and proposed writes.
- [Guard gh pr](commands/guard-gh-pr.md) - The hook that blocks raw gh writes and names the tbhbdev alpha command to run instead.
- [State guard](commands/state-guard.md) - The Claude Code hook that blocks sign-off and co-author trailers and open-note items without decisions.
- [Session context](commands/session-context.md) - The SessionStart hook that reports the host, its role, and the top commands.
- [Guard gh poll](commands/guard-gh-poll.md) - The hook that blocks GitHub polling, force pushes and git hook bypasses.
- [Guard scripts](commands/guard-scripts.md) - The hook that keeps operator scripts in a fixed directory with fixed names.
- [gh shim](commands/gh-shim.md) - The gh on PATH that budgets agent sessions' gh calls.
- [Guard gh config dir](commands/guard-gh-config-dir.md) - The hook that blocks gh commands when GH_CONFIG_DIR is unset or empty.
- [Agent env](commands/agent-env.md) - Running Codex or Antigravity with the gh shim first on PATH.
- [Pull request commands](commands/pr.md) - Checking, creating, editing, readying, rerunning, closing and rebasing pull requests, and reading their checks.
- [Pull request stats](commands/pr-stats.md) - One JSON row per merged or closed pull request, with cycle time.
- [Review commands](commands/review.md) - Review drafts, threads, policy checks and the run host's review posting.
- [Version check](commands/version-check.md) - Read-only comparison of the installed build on each host.
- [GitHub App token](commands/gh-app.md) - GitHub App installation tokens read through Connect.
- [Repository commands](commands/repo.md) - Repository bootstrap.
- [Merge](commands/merge.md) - Merging an approved pull request.
- [Loop](commands/loop.md) - Configuration validation, the shadow runner, and the store.
- [Agent](commands/agent.md) - Run coordination, host precheck, signing parity, fetch refs and build run expectations.
- [Verify range](commands/verify-range.md) - Per-commit attribution and signature checks over a range.
- [Brief](commands/brief.md) - Peer-run brief skeletons and checks.
- [Issue commands](commands/issue.md) - Creating issues, sub-issues and dependencies.
- [Install](commands/install.md) - Git hooks, user-level harness hooks, the gh shim and the ccstatusline pin.
- [Repository token library](commands/repo-token.md) - The library that requests a repository-restricted token for the ops deployer.

The alpha commands write each worktree's state under `$(git rev-parse --git-dir)/tbhb-alpha`, so sessions in two worktrees of one clone keep separate state.
