---
description: Shared GitHub content validation and writes for plugins.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    effort: medium
    surface: exec
    session: sha-256-64:7e4e81139716a7b7
    at: "2026-11-23T04:19:24Z"
status: stable
tags: [plugins]
title: Content writes
type: Reference
---

# Content writes

`tbhbdev.content` is the shared library for GitHub content validation and for writes by the `pm` and `sm` plugins. Import it directly.

- `render(template_path, data)` replaces `{{key}}` placeholders with mapping values. A missing key raises `KeyError`.
- `create_issue(repo, title, body, *, issue_type, labels=(), fields=None, project=None, client=None)` creates an issue with its type and labels. `repo` is `owner/repo`. When given a project, it adds the issue and sets the fields after creation.
- `add_comment(ref, text, *, client=None)` posts a comment.
- `close_issue(ref, reason, *, comment=None, client=None)` accepts `completed`, `not-planned`, or `duplicate`. It posts an optional comment before closing; a refused comment leaves the issue open.
- `link_sub_issue(parent, child, *, client=None)` reads the child's database ID and links it to the parent, including across repositories in the organization.
- `add_to_project(ref, project, fields=None, *, client=None)` adds an issue to a Projects v2 project. `project` is the project node ID. `fields` maps field node IDs to GraphQL field values, such as `{"FIELD_ID": {"singleSelectOptionId": "OPTION_ID"}}`.

`IssueRef(owner, repo, number)` is a frozen dataclass. Every write accepts an injected `Client` with `rest(method, path, payload=None)` and `graphql(query, variables)` methods returning dictionaries. The default `GhClient` runs `gh api` with JSON input and inherits gh's environment credentials. API errors propagate to the caller. Earlier successful writes remain applied if a later step fails.

## Review validation

`tbhbdev.content.check_review(markdown: str) -> list[str]` takes the Markdown body of a pull request review. An empty list means it passed. On refusal, the list contains a diagnostic. The function normalizes the body as object `pr-review`, variant `review`, and checks it against `#Review` with the binary from `tbhbdev cue install`. The caller verifies the commit hash against the pull request head before posting.

Start with one paragraph of five consecutive header lines. Use one token each for the run name, provider, and model. `Head` takes a 40-digit hexadecimal commit hash and `Tier` takes `1`, `2`, or `3`. Follow the header with `Verdict` and `Findings` sections in that order.

```markdown
Run: review-build
Provider: openai
Model: gpt-6
Head: 0123456789abcdef0123456789abcdef01234567
Tier: 1

## Verdict

HOLD

## Findings

- BLOCK `src/app.py:4` Return the saved value when the cache is warm.
```

Each finding is one bullet with a class (`BLOCK`, `TECH-DEBT`, or `SECURITY-DEBT`), a whitespace-free `file:line` reference with a positive line number, and one or two sentences. Use a period, question mark, or exclamation point at the end of each sentence. The checker treats whitespace after that punctuation as a sentence boundary and applies the shared CI restatement check to each sentence. Limits are 40 visible prose words per finding, 8 findings, and 160 visible prose words across all findings. The checker counts prose after the class and reference.

Use `HOLD` with at least one `BLOCK` finding. With `GO`, use debt findings or leave `Findings` empty.

## Caller and dry run

Every write accepts the keyword arguments `caller="tbhbdev"` and `dry_run=False`. The caller identifies the plugin in each audit entry, including nested comments and project writes. Existing calls keep their return values: `create_issue` returns an `IssueRef`, and other writes return `None`.

With `dry_run=True`, text is scanned, no REST or GraphQL write is sent, and no audit line is written. The return value is a dictionary containing the payload that would be sent. `link_sub_issue` and `add_to_project` still perform a read to obtain the existing issue's ID. Multi-write previews use these keys:

- Closing with a comment returns `{"comment": {"body": "..."}, "issue": {"state": "closed", "state_reason": "..."}}`.
- Adding to a project returns `add_item` with a GraphQL `query` and `variables`, plus `set_fields`, a list of GraphQL payloads. Field updates use `<created-project-item-id>` for the ID returned by the addition.
- Creating with a project returns `issue` with the REST payload and `project` with the project preview above. The addition uses `<created-issue-node-id>` for the ID returned by issue creation. Without a project, creation returns the REST payload directly.

## Decision records

```python
check_decision_record(markdown: str) -> list[str]
```

Pass the Markdown body starting with a hash-prefixed H1 title. The caller removes frontmatter. An empty list means the body passes. Each string in a nonempty list describes a refusal.

The title needs at least four visible words and cannot end in a colon or question mark. The H2 sections are Context, Decision, Options considered (optional), and Consequences, in that order. These are the only headings after the title. The word caps for Context, Decision, and Consequences are 150, 120, and 150 respectively. Options considered can hold up to five flat bullets of at most 25 words each. The 450-word body cap uses visible text and excludes the title and section headings. [Content model](content-model.md)

Write each paragraph on one source line, including paragraphs within bullets. A sentence cannot start a line as a quotation. Tables, code blocks, and block quotes are forbidden.

## Secret scan

`scan_text(text, what)` runs the same scan without rendering, sending, or auditing anything. Plugins can use it before committing a record.

The library scans exact title, body, and comment text before sending it, through `tbhbdev.alpha.secret_scan.scan`. A refusal raises `Refused` with the scanner's diagnostic and sends no write. The library never rewrites refused text. Tests inject a recording client and replace the scanner; they need neither network access nor gitleaks.

The scan uses the current Git worktree root and finds the real gitleaks binary through the existing tool lookup. It requires the host's private gitleaks config and the worktree's `.gitleaks.toml`. It supplies unknown visibility, which applies all private rules even for a private destination. See [Secret scan](alpha/secret-scan.md) for scanner configuration.

## Audit log

Every successful REST or GraphQL write appends a JSON line to `TBHBDEV_AUDIT_LOG`, defaulting to `~/.local/state/tbhb/content-audit.jsonl`. Entries contain `time` in UTC, `verb`, `caller`, `repo` as `owner/repo`, `number`, and `scan`: `pass` for scanned text writes or `not-needed` for writes without text. Titles and bodies never appear in the log. A refused scan produces no audit entry. A project addition and each field update get separate entries.

## Deferred work

Tech debt: `edit_issue` and `write_pr_text` follow in a second PR. Retries, rate-limit handling, sub-issue removal, project and field name lookup, destination visibility lookup, extra validation, and hostile-input handling are outside this version.

Security debt: App permission narrowing, guard hooks, audit file hardening, and concurrent audit writes remain deferred. An audit append can fail after GitHub has accepted a write.
