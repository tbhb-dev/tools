---
base: main
---

# feat(issue): check staged issue blocks

## Why

Operators need to catch invalid issue types, counts, and references in a staged import before an import creates GitHub issues.

## Behavior change

`tbhbdev issue import FILE --check` reads Markdown issue blocks and reports each refusal by block ID, rule, and count. It reports title shape as a warning. Validation covers the issue taxonomy, acceptance and verification counts, required parent types, missing references, and relationship cycles. When validation finds a refusal, the command reports every finding and exits with status 1.

## Decisions

The checker rejects duplicate reference cycles so each duplicate can resolve to one kept issue.

## Verification

- `bazelisk test //... --strategy=TestRunner=local --test_env=PATH` passed all 29 repository test and lint targets.
- `bazelisk run @tbhb_rules//lint:pr_size_count -- origin/main HEAD` counted 369 code lines and 489 raw lines.
- `pytest -q tests/test_issue.py` passed 23 tests covering validation, diagnostics, and count limits.
