---
base: main
draft: true
---

# fix(alpha): report skipped build pushes

## Why

Builds that committed on another branch reported success. The commits stayed in the run clone, and the driver stopped before opening a pull request.

## Behavior change

When the harness succeeds and only another local branch or a detached `HEAD` has new commits, the driver logs where the commits are and exits with status 4. A successful build with new commits also exits with status 4 when the push token is unavailable. The log records the error class. Reviews, builds without new commits, and harness failures keep their exit status.

## Risk

If the harness removes every ref to a commit and moves `HEAD` away from it, the driver skips that commit.

## Verification

`uv run --locked pytest` passed 1,061 tests with 100% line and branch coverage in the changed modules. The `mutmut run` checks rejected all 433 targeted mutants. `uv run --locked ruff check`, `uv run --locked ruff format --check`, and `uv run --locked pyrefly check --python-platform linux` passed for the changed Python files.
