---
description: Pull request authorship, approval guards, and closing commands.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: No self-approval
type: Reference
---

# No self-approval

A session can't approve a pull request that it, or one of its subagents, opened. The rule compares root session hashes, the `session=` value in the trailers, which a subagent shares with its root.

- `tbhbdev alpha pr create` writes `Opened-by: LLM …` as the first line of the body's trailer paragraph. It holds the opener's root identity, with no `subagent=`. `pr edit` copies the line from the live body unchanged, `pr check` rejects a draft that contains one, and `tbhbdev alpha merge` drops it from the squash commit. When the tool can't find an agent's root, as for an Antigravity child with no brain directory, it doesn't write the line. It also skips the line and the ledger entry, with a warning, when the root's value fails the trailer check, such as one without `model=`. The merge refuses a body with an `Opened-by` line anywhere outside the final trailer paragraph, fenced code included.
- Once `gh` opens the pull request, `pr create` adds `owner/repo#N` to the opener's ledger, `opened/<root hex>.json` in `$XDG_STATE_HOME/tbhbdev/tbhb-alpha`, which defaults to `~/.local/state/tbhbdev/tbhb-alpha`.
- `guard-gh-pr` denies `gh pr review N --approve`, a `gh api` POST to `repos/O/R/pulls/N/reviews` with `event=APPROVE`, and `tbhb-approve REPO N SHA` when the caller's ledger lists that pull request. Without `-R`, the repository comes from a `GH_REPO=` prefix, else the origin of the hook's directory. After a `cd`, `pushd`, `popd` or `git -C` in the same command line, that origin is unknown. An unreadable ledger, a session it can't identify, or, for a session that opened anything, a pull request it can't name denies too. A session with no ledger file opened nothing and may approve.
- `tbhbdev alpha pr close N --reason TEXT [--delete-branch] [--dry-run]` closes an open pull request, and `tbhbdev alpha issue close N --reason completed|not-planned [--comment TEXT] [--dry-run]` closes an open issue. A blank `--reason` or `--comment` is a usage error. Both act through the agent's own `gh` and leave a final comment that holds the reason and the agent's `Generated-by` trailer, and both refuse a caller that isn't an agent session. `pr close` refuses a pull request with no `Opened-by` line unless its GitHub author is the account the agent's `gh` runs as, and then its comment records that basis. It notes in its comment when the closer isn't the opener. With `--delete-branch`, it then deletes the head branch, unless the branch is on a fork, which it leaves alone and says so. Before it comments or closes, `--delete-branch` lists the open pull requests that target the head branch and refuses, naming them, when any exist or when `gh` can't list them. That policy is provisional until the alpha-review proposal settles open question 11.
- `tbhbdev alpha pr ready N [N...]` marks each pull request ready for review through `gh pr ready`. It refuses unless the pull request's GitHub author is the account the agent's own `gh` runs as. It doesn't read a draft.
- `tbhbdev alpha pr backfill-opened OWNER` covers the pull requests opened before the ledger existed. It reads every pull request, open, closed and merged, in every repository of the owner, and adds each one to the ledger of every `session=` its `Generated-by`, `Assisted-by` and `Opened-by` lines name. Run it once on each host. A rerun doesn't change anything. It warns when a listing reaches its `--limit`.

The guard reads only this host's ledger and sees only the commands it scans, so this isn't a security boundary.
