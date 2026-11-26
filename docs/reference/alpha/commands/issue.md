---
description: Creating issues, sub-issues and dependencies.
generated:
  - role: generated
    harness: claude-code:2.1.319
    model: anthropic:claude-sonnet-5-5
    effort: high
    surface: sdk-cli
    session: sha-256-64:e23dce32b8825437
    at: "2026-11-26T03:16:54Z"
status: stable
tags: [alpha]
title: Issue commands
type: Reference
---

# Issue commands

`tbhbdev alpha issue create --repo OWNER/REPO --type TYPE --title TEXT --body-file FILE` creates a GitHub issue from a recognized agent session.
Repeat `--label L` to add labels.
Use `--parent N` for a parent in the same repository or `--parent OWNER/REPO#N` for a parent in another repository.
`--reason TEXT` adds a note to the local audit caller entry.
The command prints the issue URL and number before attempting the parent link.
A link refusal leaves the created issue open and exits with an error.

For types that `tbhbdev issue import --check` accepts, write the body using staged issue fields such as `criteria:` or `verification:` with list items and `evidence:` for supporting text.
Supply the type, title, and parent as command options.
Research and Decision skip the importer check.
The shared content scans apply to every type.
With `--dry-run`, the command prints the creation payload as JSON and skips the write, parent link, and audit entry.

`tbhbdev alpha issue link-sub PARENT CHILD` adds a sub-issue, and `unlink-sub PARENT CHILD` removes it.
A private issue can have a public sub-issue only when the parent is a Feature and the child is a Task, Defect, Chore, Spike, or Research issue.
`block BLOCKED --by BLOCKER` adds a dependency, and `unblock BLOCKED --by BLOCKER` removes it.
Use `--blocked-by` as an alias for `--by`, or pass `BLOCKER --blocking BLOCKED` to specify the blocker first.

Use `owner/repo#N` for a qualified issue reference or `'#N'` for an issue in the current repository's origin.
GitHub requires the same repository owner for the parent and child when adding a sub-issue.
With `--dry-run`, the command looks up the related issue and prints its numeric ID as JSON.
This mode skips the write and audit entry.
