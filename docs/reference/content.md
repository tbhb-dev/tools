---
description: Shared GitHub issue and project writes for plugins.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-22T19:03:09Z"
status: stable
tags: [plugins]
title: Content writes
type: Reference
---

# Content writes

`tbhbdev.content` is the shared library for GitHub content writes by the `pm` and `sm` plugins. Import it directly; it has no CLI command or templates.

- `render(template_path, data)` replaces `{{key}}` placeholders with mapping values. A missing key raises `KeyError`.
- `create_issue(repo, title, body, *, issue_type, labels=(), fields=None, project=None, client=None)` creates an issue with its type and labels. `repo` is `owner/repo`. When given a project, it adds the issue and sets the fields after creation.
- `add_comment(ref, text, *, client=None)` posts a comment.
- `close_issue(ref, reason, *, comment=None, client=None)` accepts `completed`, `not-planned`, or `duplicate`. It posts an optional comment before closing; a refused comment leaves the issue open.
- `link_sub_issue(parent, child, *, client=None)` reads the child's database ID and links it to the parent, including across repositories in the organization.
- `add_to_project(ref, project, fields=None, *, client=None)` adds an issue to a Projects v2 project. `project` is the project node ID. `fields` maps field node IDs to GraphQL field values, such as `{"FIELD_ID": {"singleSelectOptionId": "OPTION_ID"}}`.

`IssueRef(owner, repo, number)` is a frozen dataclass. Every write accepts an injected `Client` with `rest(method, path, payload=None)` and `graphql(query, variables)` methods returning dictionaries. The default `GhClient` runs `gh api` with JSON input and inherits gh's environment credentials. API errors propagate to the caller. Earlier successful writes remain applied if a later step fails.

## Secret scan

The library scans exact title, body, and comment text before sending it, through `tbhbdev.alpha.secret_scan.scan`. A refusal raises `Refused` with the scanner's diagnostic and sends no write. The library never rewrites refused text. Tests inject a recording client and replace the scanner; they need neither network access nor gitleaks.

The scan uses the current Git worktree root and finds the real gitleaks binary through the existing tool lookup. It requires the host's private gitleaks config and the worktree's `.gitleaks.toml`. It supplies unknown visibility, which applies all private rules even for a private destination. See [Secret scan](alpha/secret-scan.md) for scanner configuration.

## Audit log

Every successful REST or GraphQL write appends a JSON line to `TBHBDEV_AUDIT_LOG`, defaulting to `~/.local/state/tbhb/content-audit.jsonl`. Entries contain `time` in UTC, `verb`, `repo` as `owner/repo`, `number`, and `scan`: `pass` for scanned text writes or `not-needed` for writes without text. Titles and bodies never appear in the log. A refused scan produces no audit entry. A project addition and each field update get separate entries.

## Deferred work

Tech debt: `edit_issue` and `write_pr_text` follow in a second PR. Retries, rate-limit handling, sub-issue removal, project and field name lookup, destination visibility lookup, extra validation, and hostile-input handling are outside this version.

Security debt: App permission narrowing, guard hooks, audit file hardening, and concurrent audit writes remain deferred. An audit append can fail after GitHub has accepted a write.
