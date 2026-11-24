---
base: main
---

# feat(alpha): launch and summarize paired reviews

## Why

Review sessions spend context checking each run for completion. The command should collect reviews of one pull request head and return the results together.

## Behavior change

`tbhbdev alpha review pair SPEC.json` accepts two or three runs with distinct harnesses and a shared repository, head, and pull request number. It validates the spec and requires an open pull request ready for review at that head before launch. Each run uses `agent exec --profile review` and `agent wait` in parallel. Output includes each run's head, state, verdict, finding counts, and log command. Launch failures leave the other runs running. A changed or unreadable head after completion voids all verdicts. The duplicate check now permits runs whose launcher, harness, or model differs, while retaining the existing variant exception.

## Decisions

Chose distinct harnesses over repeated runs with one harness because the spec requires independent providers. Chose to wait after launch exit 75 or an answer reporting completion because a lost launch acknowledgment or an early finish can leave a result to collect.

## Risk

Exit zero means every run reported `rc=0` and the final head matched. It doesn't require a GO verdict or a parsed verdict. Callers must read the verdicts and findings. The broader duplicate exception can allow more concurrent runs on one commit when the launcher, harness, or model differs.

## Verification

`git diff --check origin/main...HEAD` passed. Read the branch diff and command source to check the description against this head. This draft refresh includes no test suite or live review execution. The draft check reports 609 code lines against a limit of 400 for this branch.
