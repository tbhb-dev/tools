---
description: Checking, creating, editing, readying, rerunning, closing and rebasing pull requests, and reading their checks.
status: stable
tags: [alpha]
title: Pull request commands
type: Reference
---

# Pull request commands

`pr check [DRAFT]` checks a draft, and `pr create [DRAFT]` opens a pull request from it.
Both default to `.github/PULL_REQUEST_DRAFT.md`.
`pr edit DRAFT --pr N` updates pull request N and requires the draft path.
It reads the LLM trailers from the pull request's commits on GitHub and refuses the edit if it cannot list those commits.
`pr ready N [N...]` marks each one ready for review, only when the pull request's GitHub author is the account the agent's own `gh` runs as.
`pr rerun N` reruns only the failed jobs of the latest run for pull request N's current head commit.
It reports an error when that run hasn't failed or no run exists.
`pr close N --reason TEXT` (or `--comment TEXT`) closes it after the agent-session, open-state and opened-ledger checks, in origin's repository.
`pr delete-branch N` deletes only the head branch of a closed or merged pull request in origin's GitHub repository.
It refuses fork heads, `main`, protected branches, and unreadable metadata.

`tbhbdev alpha pr rebase N [N...]` asks Renovate to rebase open pull requests in the repository named by `origin`.
It ticks the rebase checkbox only for pull requests from the Renovate bot and preserves the rest of each body.
It skips checked boxes and stops at the first failure.

`tbhbdev alpha pr check` and `pr create` run the rules size counter after linting the draft, comparing the draft's base branch on `origin` with `HEAD`.
A missing remote-tracking ref is fetched once.
It prints the added code and raw line counts with their limits: 400 code lines and 1,500 raw lines.
If either count exceeds its limit, the check fails with a message to split the change into stacked pull requests.
When both counts exceed their limits, the problem reports the code count.
If the counter is unavailable or returns an error or invalid JSON, the command prints `size count unavailable: <reason>` as a warning and continues, and `pr create` still opens the pull request.

`tbhbdev alpha pr checks N` reads pull request N in the current repository.
It prints the head commit hash on the first line, then one `name: CONCLUSION` line per check, including commit statuses.
Unfinished checks (`PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED` or a pending commit status) print `PENDING`.
Exit code 0 means every check is green: `SUCCESS`, `NEUTRAL` or `SKIPPED`.
Exit code 1 means a failed check, any value the command doesn't know (including lowercase ones, a commit status of `EXPECTED`, or a completed check with no conclusion), or no checks at all.
Exit code 3 means nothing failed but a check is still unfinished.
A check run that hasn't completed reads its status and never its conclusion.
A commit status is green only for `SUCCESS`.
A missing pull request, a failed read or a check with no name exits nonzero with an error.
