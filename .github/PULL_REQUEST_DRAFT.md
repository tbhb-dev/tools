---
base: main
---

# fix(alpha): publish the tip of a run's branch chain

## Why

A build run can make a commit on one branch and continue on another. Publication treated both branch tips as separate work and skipped the push even though the later tip contained both commits.

## Behavior change

The driver considers only local branch tips and detached `HEAD`. Candidates must descend from the start commit and fall outside upstream and pre-run history. The driver drops any tip with a descendant among the candidates. When one distinct tip remains, it pushes that tip to the run branch. If two or more remain, it exits with code 4. It skips tags, notes, and stashes.

## Risk

Tier 2. If the Git history check fails or times out, the driver exits with code 4 before pushing.

## Verification

The tests verify exit codes and published refs for branch chains, diverged branches, fetches, tags, and stashes against a local bare repository. Mutation testing killed all 152 mutants in the changed functions.
