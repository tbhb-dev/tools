---
description: Shared GitHub issue and project writes for plugins.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.1-sol
    at: "2026-11-22T19:26:11Z"
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
- `reopen_issue(ref, *, comment=None, client=None)` posts an optional comment before reopening with `state: open`. A refused comment leaves the issue closed.
- `edit_issue(ref, *, body=None, title=None, issue_type=None, fields=None, client=None)` patches the supplied REST fields. Omitted text stays unchanged; an empty string clears it. `fields` is accepted but Project field updates are deferred because this interface has no known Project item ID.
- `write_pr_text(owner, repo, number, *, body=None, title=None, comment=None, client=None)` patches the supplied pull request title and body at `/repos/{owner}/{repo}/pulls/{number}`, then posts an optional issue comment. All supplied text is scanned before either write. With no text supplied it sends no writes.
- `link_sub_issue(parent, child, *, client=None)` reads the child's database ID and links it to the parent, including across repositories in the organization.
- `add_to_project(ref, project, fields=None, *, client=None)` adds an issue to a Projects v2 project. `project` is the project node ID. `fields` maps field node IDs to GraphQL field values, such as `{"FIELD_ID": {"singleSelectOptionId": "OPTION_ID"}}`.

`IssueRef(owner, repo, number)` is a frozen dataclass. Every write accepts an injected `Client` with `rest(method, path, payload=None)` and `graphql(query, variables)` methods returning dictionaries. The default `GhClient` runs `gh api` with JSON input and inherits gh's environment credentials. API errors propagate to the caller. Earlier successful writes remain applied if a later step fails.

## Caller and dry run

Every write accepts the keyword arguments `caller="tbhbdev"` and `dry_run=False`. The caller identifies the plugin in each audit entry, including nested comments and project writes. Existing calls keep their return values: `create_issue` returns an `IssueRef`, and other writes return `None`.

With `dry_run=True`, text is scanned, no REST or GraphQL write is sent, and no audit line is written. The return value is a dictionary containing the payload that would be sent. `link_sub_issue` and `add_to_project` still perform a read to obtain the existing issue's ID. Multi-write previews use these keys:

- Closing with a comment returns `{"comment": {"body": "..."}, "issue": {"state": "closed", "state_reason": "..."}}`. Reopening uses the same shape with `{"state": "open"}` as the issue payload.
- Writing pull request text with a comment returns `pull_request` with the REST patch payload and `comment` with the comment payload. A comment-only preview has an empty `pull_request` dictionary and sends only the comment when executed.
- Adding to a project returns `add_item` with a GraphQL `query` and `variables`, plus `set_fields`, a list of GraphQL payloads. Field updates use `<created-project-item-id>` for the ID returned by the addition.
- Creating with a project returns `issue` with the REST payload and `project` with the project preview above. The addition uses `<created-issue-node-id>` for the ID returned by issue creation. Without a project, creation returns the REST payload directly.

## Secret scan

`scan_text(text, what)` runs the same scan without rendering, sending, or auditing anything. Plugins can use it before committing a record.

The library scans exact title, body, and comment text before sending it, through `tbhbdev.alpha.secret_scan.scan`. A refusal raises `Refused` with the scanner's diagnostic and sends no write. The library never rewrites refused text. Tests inject a recording client and replace the scanner; they need neither network access nor gitleaks.

The scan uses the current Git worktree root and finds the real gitleaks binary through the existing tool lookup. It requires the host's private gitleaks config and the worktree's `.gitleaks.toml`. It supplies unknown visibility, which applies all private rules even for a private destination. See [Secret scan](alpha/secret-scan.md) for scanner configuration.

## Audit log

Every successful REST or GraphQL write appends a JSON line to `TBHBDEV_AUDIT_LOG`, defaulting to `~/.local/state/tbhb/content-audit.jsonl`. Entries contain `time` in UTC, `verb`, `caller`, `repo` as `owner/repo`, `number`, and `scan`: `pass` for scanned text writes or `not-needed` for writes without text. Titles and bodies never appear in the log. A refused scan produces no audit entry. A project addition and each field update get separate entries.

## Deferred work

Tech debt: `edit_issue(fields=...)` accepts but does not apply Project fields; looking up the issue's Project item is deferred. Dry-run project previews use placeholders for IDs that only a successful write can allocate. Retries, rate-limit handling, sub-issue removal, project and field name lookup, destination visibility lookup, extra validation, and hostile-input handling are outside this version.

Security debt: App permission narrowing, guard hooks, audit file hardening, and concurrent audit writes remain deferred. An audit append can fail after GitHub has accepted a write.
