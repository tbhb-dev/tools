---
base: main
---

# feat(content): vet normalized drafts with cue

## Why

Plugins need a shared check before writing content. The check needs to point authors to the field that failed and explain how to fix it. [Pinned cue runtime](https://github.com/tbhb-dev/tools/pull/303)

## Behavior change

`content.check` vets normalized drafts against the shared schemas and a caller-selected variant. It returns findings with field paths and revision guidance. A Feature requires an Epic parent, and an Epic requires an Initiative. Title shapes and criteria that appear to join two checks draw warnings. For CI restatements, the tool suggests describing an observable outcome.

`close_issue` reads the issue body, including during a dry run. An unchecked acceptance box stops both the close request and its optional comment.

## Decisions

Callers supply the schema directory and look up the parent type. Bazel tests read the runtime pin to fetch the same `cue` release.

## Risk

A concurrent edit can change the checklist between the read and the close request. The diagnostic parser depends on the pinned `cue` output. Strict expected failures track the cases that the CI pattern misses.

## Verification

`pytest` passed 68 content tests with 100% branch coverage. The tests run the same examples through the acceptance and verification rules and record six expected failures. `pyrefly check` passed.
