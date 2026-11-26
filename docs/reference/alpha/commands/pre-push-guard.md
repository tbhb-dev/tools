---
description: Refusals for pushes to main and history rewrites, and signature requirements.
generated:
  - role: generated
    harness: codex:0.166.0
    model: openai:gpt-6.1-sol
    surface: exec
    session: sha-256-64:bb854b7878737d0a
    at: "2026-11-26T07:14:41Z"
status: stable
tags: [alpha]
title: Pre-push guard
type: Reference
---

# Pre-push guard

`pre-push-guard REMOTE URL` refuses every push to `main` or `master`, and refuses a non-fast-forward push unless the remote tip git reports is the tip in `refs/remotes/REMOTE/BRANCH` of a branch under `refs/heads/`.
It logs `REF rewritten OLD -> NEW` on stderr for each rewrite it allows.
Because the hook cannot see which flag was used, the command-line guard (`guard-gh-poll`) allows only `git push --force-with-lease` to branches other than `main` and `master`, and denies `--force`, `-f`, `--mirror`, `+REF` refspecs and `--force-if-includes` without a lease.
The installed pre-push hook runs it before prek with the same arguments and input.
In a run clone, the hook sets `TBHBDEV_RUN_NAME` and the guard accepts only `refs/heads/agent/NAME` as a branch destination.
Nonempty `SKIP` or `PREK_SKIP` refuses the push.
Install fails when a foreign pre-push hook exists and asks you to move it aside.
No setting bypasses the guard.
Git’s `--no-verify` skips all hooks.
Standing rules prohibit it.

The pre-push guard requires each newly pushed commit with an agent author or committer to have a valid SSH signature from the local agent key or a signing key registered to `tbhb-agent` on GitHub.
The guard accepts local signatures offline.
When it needs the registered keys, it makes one request per push with a five-second timeout.
If the request fails, the guard accepts the local key alone and explains the fallback when it refuses a commit.

From a detected agent session, every branch creation or update must target a remote branch under `agent/` or `renovate/`, including when the push sends no new commits.
Agents may delete only `agent/` branches.
The guard names each refused ref.
