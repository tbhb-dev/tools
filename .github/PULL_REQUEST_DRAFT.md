---
base: main
---

# feat(content): check the body of a decision record

## Why

The PM plugin needs a shared way to check decision records before it writes them. The decision record format defines the title, section order, and length limits.

## Behavior change

`tbhbdev.content.check_decision_record(markdown)` returns a list of refusals for body violations and an empty list when the body passes. It checks the title, required sections, options, word limits, and disallowed Markdown blocks. It also identifies wrapped paragraphs and quoted sentences that start a line. Callers remove frontmatter before passing the body.

## Verification

The focused content suite passed 104 tests with 100% branch coverage. The new tests detected all 173 mutants in the changed functions.

`bazelisk test //...` passed 28 targets, including `pyrefly`. The Python target reported 100% branch coverage and 9 host test failures. Those failures reproduced on the base commit.
