---
description: The issue label and field configuration, its merge rules, enforcement, label sync, and build order.
generated:
  - role: generated
    harness: claude-code:2.1.330
    model: anthropic:claude-opus-5-5
    effort: high
    surface: sdk-cli
    session: sha-256-64:0639582cdb1b6031
    at: "2026-11-23T14:34:39Z"
status: draft
tags: [issues, labels, configuration, design]
title: Issue label and field configuration
type: Design Draft
---

# Issue label and field configuration

The issue write commands in `tbhbdev.content` accept only the labels, field values, and board Project that the merged configuration of the target repository allows. Two TOML files make up that configuration, and one CUE schema defines both.

## Labels and fields

A label reads `family/value`. The configuration defines three families:

- `area/` names a subsystem. The global file lists the shared values, and a repository file adds its own.
- `concern/` names a cross-cutting reason, such as `concern/security`. The global file lists most values, and a repository file may add more.
- `role/` names the kind of agent role that owns the issue. Its values come from the roles registry, and never name a repository or a session.

No label repeats what an issue type or a field carries, and no label marks technical or security debt. Security work carries `concern/security`.

The fields are the organization's issue fields: Priority, Work type, Severity, Handler, Source, and Status. Handler applies to Requests only. Each value matches the live option name byte for byte, in sentence case, such as `Needs info` and `Operator ask`. Source has no `Import` value.

## Files

| File | Owner | Location in each repository |
| --- | --- | --- |
| Global configuration | tbhb-dev/rules, in its baseline | `.github/issues/global.toml` |
| Repository configuration | The repository | `.github/issues/repo.toml` |
| Schema | tbhb-dev/rules, in its baseline | `.github/schemas/issue-config.cue` |
| Editor schema | tbhb-dev/rules, which generates it from the CUE schema | `.github/schemas/issue-config.schema.json` |

A repository never edits its baseline copies. The tombi configuration maps both TOML files to the generated JSON Schema. Rules generates that file with the pinned cue, and a rules gate fails when it differs from the committed copy.

Rules defines content committed as Markdown, pm defines issue content, and tools keeps the validation, write, and sync code.

## Format

```toml
# .github/issues/global.toml
version = 1
private_topics = ["area/identity", "area/secrets"]

[projects]
default = "work"
repos = { import-calibration = "work-calibration" }

[labels.area.identity]
description = "Agent identity and commit trailers"
color = "1d76db"

[labels.role]
source = "roles-registry"

[fields]
"Priority" = ["Expedite", "Standard", "Intangible"]
"Handler" = ["Agent", "Operator"]

[types.Task]
labels = ["area/*", "concern/*", "role/*"]
fields = { "Priority" = ["Expedite", "Standard", "Intangible"] }

[types.Request]
labels = ["area/*", "role/*"]
fields = { "Handler" = ["Agent", "Operator"] }
```

The schema enforces these constraints:

- `version` equals `1`.
- A family name is one of `area`, `concern`, or `role`, and a value matches `^[a-z0-9]+(-[a-z0-9]+)*$`.
- `description` has 1 to 100 characters and `color` has six lowercase hex digits.
- `role` holds only `source = "roles-registry"`.
- `fields` maps each field name to the complete list of its option names, without duplicates.
- A type's `labels` entry reads `family/*` or `family/value`, and each type's field values form a subset of `fields`.
- `private_topics` entries name labels the same file defines.
- A repository file holds only `version`, `labels.area`, `labels.concern`, `private_topics`, and `types.<Type>.labels`.

## Merge rules

tools validates each file against the schema with the pinned cue, then merges the repository file over the global file. Validation and merging fail closed: any error refuses every write and sync for that repository.

- A repository may add `area/` and `concern/` values, add its own labels to a global type's `labels` list, and add `private_topics` entries.
- A repository may not add a family, a `role/` value, a field, a field value, an issue type, or a project mapping.
- A repository may not change the description or color of a global label, or remove a global label, type entry, or private topic.
- A repository label with the same name as a global label is a conflict, even with identical content. When rules adds a global value that a repository already defines, the baseline sync pull request fails the repository's configuration check until the repository deletes its copy.

## Lookup

tools reads the global file and the schema from the root of the current worktree, as it reads the other baseline files, with no network call and no fallback to a packaged copy or to rules. Outside a worktree, or with either file missing, it refuses.

For the current repository, tools reads `repo.toml` from the worktree. For another target repository, tools reads `repo.toml` at the head of the default branch through the contents API.

`tbhbdev issue config check` runs offline and validates and merges the two files of the current worktree. `--live` adds the organization comparison.

## Live option names

tools reads the organization's issue field definitions and their option names through the GitHub API at each write and on `config check --live`. It keeps no cache.

- A write refuses a field or value with no exact live match, and resolves names to node IDs only from that read.
- `config check --live` fails on drift in either direction: a configured field or value with no live match, or a live option the global file omits.

## Enforcement

`create_issue` takes field names and option names instead of node IDs, and checks before its first write, in dry run too:

1. Load and merge the configuration of the target repository.
2. Refuse an issue type the configuration doesn't list.
3. Refuse a label outside the merged label set, outside the type's `labels` list, or a `role/` value the roles registry lacks.
4. Refuse a field or value outside the type's `fields`, or with no live match.
5. Refuse a public target when any label appears in `private_topics`, and state that the issue belongs in the private twin.
6. Resolve the board Project from `projects`. Refuse a caller's `project` argument that differs from it, and refuse a title that matches no project or more than one.

A configuration refusal leaves no partial issue, because every check precedes the first write.

`add_comment` loads the merged configuration of the target repository and refuses when it is missing or invalid. It reads the issue's labels and refuses a comment on a public issue that carries a private-topic label.

## Label sync

`tbhbdev issue labels sync OWNER/REPO` reconciles the repository's labels with the merged configuration and the roles registry:

- It creates each missing label and updates each label whose description or color differs.
- It reports each label outside the configuration, including GitHub's default labels and labels in another form, and exits with status 1.
- `--prune` deletes the reported labels. `--dry-run` prints the planned calls and writes nothing.
- It writes through the content client and audit log.

## Failure modes

| Condition | Result |
| --- | --- |
| Global file, repository file, or schema missing or unreadable | Refuse, naming the path |
| A file fails the schema, or the merge finds a conflict | Refuse, with the cue or merge error |
| The pinned cue is missing | Refuse with `run tbhbdev cue install` |
| The roles registry is unreadable and the write or sync needs a `role/` value | Refuse |
| The live field read fails | Refuse the write |
| The project mapping resolves to no project or more than one | Refuse the write |
| A write after the issue creation fails | Earlier writes remain, as today |

## Build order

Each step lands as its own pull request under 400 code lines as `scc` counts them.

1. rules: the CUE schema, the generated JSON Schema and its gate, the global file, the tombi mapping, and their baseline shipping.
2. tools: the loader, schema validation, merge, and `issue config check`, offline.
3. Each repository: its `repo.toml`, one pull request per repository, before enforcement ships.
4. tools: the live field read and `config check --live`.
5. tools: enforcement in `create_issue` and `add_comment`, with field, option, and project names in place of node IDs.
6. tools: `issue labels sync`.
7. tools: the removal of the label patterns, field enumerations, and `Import` source from `taxonomy.cue`, so the configuration stays their only definition.

## Open questions

### pm

- What are the complete Source and Status option lists?
- Does pm switch to field, option, and project names in one release with step 5?
- When do `issue.cue` and `taxonomy.cue` move to pm?
- Which types carry Severity, Source, and Status?

### rules

- Where in rules do the schema and the global file live?
- How does a repository detect a stale baseline copy of the global file?
- Does the rules baseline run `issue config check` as a gate in every repository?
- Who picks label colors?

### Administrator VM session

- Which API call reads the organization's issue field options, and which token scope does it need?
- Who creates the Source and Status fields with the exact option names?
- How does tools name a repository's private twin without a public file naming it?
- How does label sync read the roles registry?
