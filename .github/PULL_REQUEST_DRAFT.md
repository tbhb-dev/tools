---
base: main
---

# fix(alpha): publish the tip of a run's branch chain

## Why

A build run can make a commit on one branch and continue on another. Publication treated both branch tips as separate work and skipped the push even though the later tip contained both commits.

## Behavior change

Publication considers local branch tips and detached HEAD. A candidate must descend from the run's start commit and be absent from upstream and pre-run history. The driver drops any candidate already reachable from another candidate. It publishes the remaining tip to the run branch, or exits with code 4 if multiple distinct tips remain. Tags, notes, and stashes are excluded from publication.

## Risk

Tier 2. If the Git history check fails or times out, the driver exits with code 4 before pushing.

## Verification

The tests verify exit codes and published refs for branch chains, diverged branches, fetches, tags, and stashes against a local bare repository. Mutation testing killed all 152 mutants in the changed functions.
