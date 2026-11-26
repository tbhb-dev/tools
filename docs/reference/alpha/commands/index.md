# Alpha command pages

* [Pre-push guard](pre-push-guard.md) - Refusals for pushes to main and history rewrites, and signature requirements.
* [Identity](identity.md) - Agent and model attribution, the Generated-by trailers, and the identity start time.
* [Guard op](guard-op.md) - The 1Password account flag requirement on shell calls and proposed writes.
* [Guard gh pr](guard-gh-pr.md) - The hook that blocks raw gh writes and names the tbhbdev alpha command to run instead.
* [State guard](state-guard.md) - The Claude Code hook that blocks sign-off and co-author trailers and open-note items without decisions.
* [Session context](session-context.md) - The SessionStart hook that reports the host, its role, and the top commands.
* [Guard gh poll](guard-gh-poll.md) - The hook that blocks GitHub polling, force pushes and git hook bypasses.
* [Guard scripts](guard-scripts.md) - The hook that keeps operator scripts in a fixed directory with fixed names.
* [gh shim](gh-shim.md) - The gh on PATH that budgets agent sessions' gh calls.
* [Guard gh config dir](guard-gh-config-dir.md) - The hook that blocks gh commands when GH_CONFIG_DIR is unset or empty.
* [Agent env](agent-env.md) - Running Codex or Antigravity with the gh shim first on PATH.
* [Pull request commands](pr.md) - Checking, creating, editing, readying, rerunning, closing and rebasing pull requests, and reading their checks.
* [Pull request stats](pr-stats.md) - One JSON row per merged or closed pull request, with cycle time.
* [Review commands](review.md) - Review drafts, threads, policy checks and the run host's review posting.
* [Version check](version-check.md) - Read-only comparison of the installed build on each host.
* [GitHub App token](gh-app.md) - GitHub App installation tokens read through Connect.
* [Repository commands](repo.md) - Repository bootstrap.
* [Merge](merge.md) - Merging an approved pull request.
* [Loop](loop.md) - Configuration validation, the shadow runner, and the store.
* [Agent](agent.md) - Run coordination, host precheck, signing parity, fetch refs and build run expectations.
* [Verify range](verify-range.md) - Per-commit attribution and signature checks over a range.
* [Brief](brief.md) - Peer-run brief skeletons and checks.
* [Issue commands](issue.md) - Creating issues, sub-issues and dependencies.
* [Install](install.md) - Git hooks, user-level harness hooks, the gh shim and the ccstatusline pin.
* [Repository token library](repo-token.md) - The library that requests a repository-restricted token for the ops deployer.
