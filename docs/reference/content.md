---
description: Shared GitHub content validation and writes for plugins.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.2-sol
    at: "2026-11-24T22:03:20Z"
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
- `close_issue(ref, reason, *, comment=None, client=None)` accepts `completed`, `not-planned`, or `duplicate`. Only for `completed`, it reads the issue body and raises `Refused` for unchecked acceptance boxes. A refused checklist or comment leaves the issue open.
- `link_sub_issue(parent, child, *, client=None)` reads the child's database ID and links it to the parent, including across repositories in the organization.
- `add_to_project(ref, project, fields=None, *, client=None)` adds an issue to a Projects v2 project. `project` is the project node ID. `fields` maps field node IDs to GraphQL field values, such as `{"FIELD_ID": {"singleSelectOptionId": "OPTION_ID"}}`.

`IssueRef(owner, repo, number)` is a frozen dataclass. Every write accepts an injected `Client` with `rest(method, path, payload=None)` and `graphql(query, variables)` methods returning dictionaries. The default `GhClient` runs `gh api` with JSON input and inherits gh's environment credentials. API errors propagate to the caller. Earlier successful writes remain applied if a later step fails.

## Review validation

`tbhbdev.content.check_review(markdown, *, metadata=None)` takes the Markdown body of a pull request review and optional parsed frontmatter. An empty list means it passed. On refusal, the list contains a diagnostic. The function normalizes the body as object `pr-review`, variant `review`, and checks it with the binary from `tbhbdev cue install`. It uses `#ReviewBody` when called with only Markdown and `#Review` when given metadata. The caller verifies the commit hash against the pull request head before posting.

Start with one paragraph of seven consecutive header lines in the order shown below. Use one token each for `Run`, `Provider`, and `Model`. `Variant` is `a` or `b`, `Host` uses lowercase dotted labels, and `Head` takes a 40-character lowercase hexadecimal commit hash. `Tier` is `0`, `1`, or `2`, with `0` for prose-only repositories. Follow the header with the H2 sections `Verdict`, `Findings`, and `Verification` in that order.

```markdown
Run: review-build
Provider: openai
Model: gpt-6
Variant: a
Host: reviewer.example.org
Head: 0123456789abcdef0123456789abcdef01234567
Tier: 1

## Verdict

NO-GO

## Findings

- BLOCK `src/app.py:4` Return the saved value when the cache is warm.

## Verification

Read the cache implementation and ran the cache tests.
```

Each finding is one bullet with a class (`BLOCK`, `TECH-DEBT`, or `SECURITY-DEBT`), a whitespace-free `file:line` reference with a positive line number, and one or two sentences. Use a period, question mark, or exclamation point at the end of each sentence. The checker treats whitespace after that punctuation as a sentence boundary and applies the shared CI restatement check to each sentence. Use at most eight findings and 40 visible prose words per body finding, counted after the class and reference.

Use `NO-GO` with at least one `BLOCK` finding. With `GO`, use debt findings or leave `Findings` empty. In `Verification`, describe what you read or ran in one or more paragraphs totaling at most 100 visible words.

Metadata requires `schema: 2`, a `repo` in `OWNER/NAME` form, a positive `pr` number, `head_sha`, `tier`, `verdict`, a `run` object with `name`, `provider`, `model`, `variant`, and `host`, and a `findings` list of objects with `class`, `path`, `line`, and `summary`. The objects reject unknown fields. The verdict, head, tier, and run fields must match the body. Findings must also match in order, with each summary equal to the visible prose of its bullet with whitespace collapsed to single spaces.

The seven-line header and `Verification` section are required. The tier range changed to `0`–`2`, and `HOLD` was renamed to `NO-GO`. The 160-word whole-review cap became a per-summary cap on metadata. The body keeps its 40-word limit on each finding, which also limits the matching summary.

## Caller and dry run

Every write accepts the keyword arguments `caller="tbhbdev"` and `dry_run=False`. The caller identifies the plugin in each audit entry, including nested comments and project writes. Existing calls keep their return values: `create_issue` returns an `IssueRef`, and other writes return `None`.

With `dry_run=True`, text is scanned, no REST or GraphQL write is sent, and no audit line is written. The return value is a dictionary containing the payload that would be sent. For `completed` only, `close_issue` reads the issue body to check its acceptance boxes. `link_sub_issue` and `add_to_project` read the existing issue's ID. Multi-write previews use these keys:

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

## Draft validation

`check(draft, schemas, *, variant_schema=None, definition="#Draft", parent_type=None)` takes a normalized draft and the path to the shared `schemas` directory. It runs `cue vet --all-errors` against `#Draft` and the selected definition. A caller can supply one additional CUE file with its variant definition. Install the pinned executable with `tbhbdev cue install`. [Markdown normalization](content-model.md)

The result has `refusals` and `warnings` tuples. Each finding has a `code`, `path`, and `message`. Use `format_refusal(finding)` for the diagnostic with revision guidance or `guide(finding)` for the guidance alone. A criterion or verification item that repeats a CI gate receives a hint to describe its observable outcome.

Once CUE accepts an issue draft, pass the actual parent issue type. A Feature requires an Epic, and an Epic requires an Initiative. Title shape and criteria that appear to join two checks with “and” produce warnings.

`check_close(draft)` returns a refusal per acceptance section with any unchecked task box. It recognizes the normalized headings “Acceptance” and “Acceptance criteria”, including repeats. The close check can run without CUE.

## Secret scan

`scan_text(text, what)` runs the same scan without rendering, sending, or auditing anything. Plugins can use it before committing a record.

The library scans exact title, body, and comment text before sending it, through `tbhbdev.alpha.secret_scan.scan`. A refusal raises `Refused` with the scanner's diagnostic and sends no write. The library never rewrites refused text. Tests inject a recording client and replace the scanner; they need neither network access nor gitleaks.

The scan uses the current Git worktree root and finds the real gitleaks binary through the existing tool lookup. It requires the host's private gitleaks config and falls back to gitleaks' defaults when the worktree has no `.gitleaks.toml`. It supplies unknown visibility, which applies all private rules even for a private destination. See [Secret scan](alpha/secret-scan.md) for scanner configuration.

## Audit log

Every successful REST or GraphQL write appends a JSON line to `TBHBDEV_AUDIT_LOG`, defaulting to `~/.local/state/tbhb/content-audit.jsonl`. Entries contain `time` in UTC, `verb`, `caller`, `repo` as `owner/repo`, `number`, and `scan`: `pass` for scanned text writes or `not-needed` for writes without text. Titles and bodies never appear in the log. A refused scan produces no audit entry. A project addition and each field update get separate entries.

## Deferred work

Tech debt: `edit_issue` and `write_pr_text` follow in a second PR. Retries, rate-limit handling, sub-issue removal, project and field name lookup, destination visibility lookup, extra validation, and hostile-input handling are outside this version.

Security debt: App permission narrowing, guard hooks, audit file hardening, and concurrent audit writes remain deferred. An audit append can fail after GitHub has accepted a write.
