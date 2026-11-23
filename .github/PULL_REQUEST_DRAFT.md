---
base: main
draft: true
---

# fix(alpha): require identity hooks for agent runs

## Why

In incident `INC-20261123-1`, agent commits lacked identity trailers. Run Git settings overrode the worktree's hook directory, and a fresh clone starts without hooks. Build agents also attempted to push with read access.

## Behavior change

Run-host installs the identity, pre-push, and prek hooks for Codex, Claude, and agy before it requests credentials. It refuses startup if Git selects a different hook directory, a required hook is missing or non-executable, or the identity command fails its dry run. The prepare-commit hook requires a harness session and a Generated-by trailer. Build prompts assign publication to the driver and tell the agent to include the PR draft in its commit.

## Risk

Token scopes stay the same. The build and review App tokens keep their read permissions, and the driver retains the write token for publication. Builds that lack the identity command or a harness session now stop at startup or commit, respectively.

## Verification

The Linux reproduction found an executable hook and a runnable command, then reported an effective hooks path of `/dev/null` and zero Generated-by trailers. `uv run pytest` measured 100% line and branch coverage in the changed modules. `mutmut` killed all 1,109 mutants in the changed functions.
