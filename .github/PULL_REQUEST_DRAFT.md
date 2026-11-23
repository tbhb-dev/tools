---
base: main
---

# feat(content): check reviews before posting

## Why

Review runs need a shared check before posting a pull request review. The posting command needs a consistent way to tie each verdict to the reviewed head.

## Behavior change

Call `tbhbdev.content.check_review(markdown)` with the review Markdown. A pass returns an empty list. Each review records the run name, provider, model, 40-digit hexadecimal head hash, and tier. Each finding has a class, file and line reference, and one or two sentences. Use at least one `BLOCK` finding for `HOLD`. With `GO`, report debt or leave the list empty. The checker reuses the CI restatement rule for issue criteria.

## Decisions

Cap findings at 40 prose words each, 8 findings, and 160 prose words across the list. These limits allow two short sentences per finding and keep the review brief. The checker counts prose after the class and reference.

## Risk

Review validation requires the pinned `CUE` binary. The sentence check treats whitespace after terminal punctuation as a boundary, so abbreviations can use up a sentence. The caller verifies that the `SHA` matches the pull request head.

## Verification

All 29 Bazel targets passed. The Python suite passed 13,795 tests and reached 100% branch coverage with a shorter Bazel output path. An installed wheel checked a sample review from outside the checkout. The shared size counter measured 161 code lines and 261 raw lines.

## Human steering

The operator assigned review content ownership to PM.
