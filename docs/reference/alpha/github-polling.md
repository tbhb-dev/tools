---
description: GitHub event delivery, polling guards, and request budgets.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: GitHub polling
type: Reference
---

# GitHub polling

Agents get GitHub CI and pull request state from awrel, which relays webhook events into a session: subscribe with the repository and the event type, such as `check_run` conclusion events. That's the way to watch GitHub state. Polling GitHub instead burns the account's REST quota, which every agent shares. A hook stops the common polls, and a shim budgets what remains. The budget is a backstop, not the way to watch GitHub state.

- `guard-gh-poll` denies `gh run watch`, `gh run list`, `gh pr checks --watch`, `gh api` or `gh query` with `--paginate`, and reads of the list endpoints `actions/runs`, `actions/workflows/W/runs`, `actions/jobs`, `check-runs` and `check-suites`. It also denies a `while`, `until` or `for` loop that runs `gh`, or runs both `sleep` and `pgrep`, such as the self-matching wait `while pgrep -f fetch.py; do sleep 5; done`. Single-object reads pass, such as `actions/runs/ID`, `pulls/N`, `commits/SHA/check-runs`, `actions/runs?head_sha=SHA` for one full commit hash, and `rate_limit`. It scans `bash -c`, `eval`, `ssh HOST COMMAND` and a here-document fed to a shell. The one exception is a granted allowance for a measurement pass: `gh run list -R OWNER/REPO --limit 100 --json FIELDS`, with the exact fields and order of the measurement runner, once for each repository in a grant file under `~/.local/state/tbhb` and not after the grant's expiry date. Repository names match in any case, and the spellings share one allowance. A grant whose `repos` isn't a list of `OWNER/REPO` strings, or that holds an empty, `.`, `..` or Unicode name, grants nothing. A count file beside it records each repository once spent, under a file lock. The guard denies `xargs` or `parallel` around a `gh run list`, because each repeat is a separate call, and it spends the allowance for a listing that follows a redirect. Only the operator writes the grant and count files. The guard denies an agent's Write or Edit to `gh-poll-allowance.json` and `gh-poll-allowance.count` in any case, or through a symbolic or hard link under another name, and any shell command that names either in any case. A shell command that reaches a file through a link under another name isn't caught. It can't see inside a script file or a computed path. The allowance only catches accidents. A call the guard scans spends its repository's call even if the shell doesn't run it. Each deny names awrel. The same hook denies `unset` and `env -u` of the 1Password service account token's variable. A command keeps the environment the harness gave it.
- `gh-shim` budgets the `gh` calls that scripts make, which no hook sees. In an agent session it refuses a call when the root session has made `[gh_budget] hourly_limit` `gh` calls in the past hour, or when the account has fewer than `core_floor` core REST requests left. It checks that with `gh api rate_limit` at most once a minute. A refusal goes to stderr and names awrel and the budget. Otherwise, and always outside an agent session, it runs the real `gh` unchanged. The state is under `$XDG_STATE_HOME/tbhbdev/gh-budget`.

Claude Code gets the shim on PATH from the SessionStart hook, which adds an `export PATH` line to `$CLAUDE_ENV_FILE`. Codex and Antigravity have no setting that prepends to PATH, so start them as `tbhbdev alpha agent-env codex` or `tbhbdev alpha agent-env agy`, followed by the harness's own arguments. A process tree that doesn't inherit that PATH, or a `gh` run by its absolute path, skips the shim. Neither mechanism is a security boundary.
