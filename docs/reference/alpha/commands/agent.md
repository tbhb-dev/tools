---
description: Run coordination, host checks, signing parity, fetch refs, and build run expectations.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: high
    surface: exec
    session: sha-256-64:352ebb64e4714f57
    at: "2026-11-26T16:32:05Z"
status: stable
tags: [alpha]
title: Agent
type: Reference
---

# Agent

`agent precheck CLONE --reference REF [--host HOST]` compares a clone's hooks with a local reference and checks its Git config for risky keys.
Paths may be absolute or relative to the home directory.
Collection uses one SSH call with `--host` and reads locally otherwise.
It never runs Git.
Exit codes are 0 for a match, 1 for mismatches, and 2 for usage or collection errors.
The Python API `precheck(clone_data, reference)` returns the result for callers.

`agent` coordinates runs with `exec`, `wait`, `status`, `log`, `diff`, `kill`, `clean` and `keep`.
`signing-parity` checks that each configured host's signing key is a signing key of tbhb-agent.
It runs the hidden host command `signing-fingerprint ACCOUNT`, which prints only the SHA256 fingerprint of the key a run would sign with, over the ordinary ssh shell.
It then reads tbhb-agent's public signing key list from GitHub and exits 1 when any host's fingerprint is missing or the host is unreachable.
A host without the command counts as unreachable until `tbhbdev` syncs there.
The package also includes the run request's schema and the repo argument rules.

When `tbhbdev alpha agent exec` finds a version mismatch, it looks up the head of `tbhb-dev/tools` on `main` and installs that commit on each side that differs.
It logs each successful install to standard error.
If it updates the driver, it re-executes the command with the original arguments.
The retry reads the host version again.
If the versions still differ, it reports the original mismatch with `after one sync` and refuses the launch.

`tbhbdev alpha agent exec --fetch-ref REF` makes `refs/heads/<branch>` available as `origin/fetched/heads/<branch>` and `refs/pull/N/head` as `origin/fetched/pull/N/head`.
Repeat `--fetch-ref` up to eight times with either profile.

`tbhbdev alpha agent exec --worker-workflow ID` launches a review as the registered worker `worker/ID` when no tmux pane names the caller.
See [Agent configuration](../agent-configuration.md) for the `[agent.worker]` table that registers it.

Build runs expect a new commit and an updated origin branch by default.
Pass `--no-expect-commit` for a research run that can finish without committing or pushing.
After a successful harness exit, `agent wait` returns 6 for a missing commit, 9 for an unchanged or unverifiable run branch on origin, and 10 for a review that the host did not post.
Use `agent status NAME` to see the reason.

Build briefs that appear to target another working branch trigger advisory warnings at launch. See [Agent isolation limits](../agent-isolation-limits.md).

For a build that updates an existing pull request, pass both `--fetch-ref refs/heads/<branch>` and `--start-ref refs/heads/<branch>`.
The run starts at the fetched commit and pushes a fast-forward to that branch without opening a pull request.
The command refuses the repository's default branch.
If the remote tip changes before the push check, the run fails and keeps its worktree.

A build run that opens a pull request writes `.github/PULL_REQUEST_DRAFT.md` in its worktree and keeps it out of commits.
Set `base` in the draft's frontmatter to identify the target branch.
The run pushes its `agent/` branch to origin and runs `tbhbdev alpha pr create` from the worktree.
The command checks the draft, adds the commit trailers and the opener's identity to the body, and records the pull request in the opened ledger.
CI fails a pull request whose diff contains the draft.
