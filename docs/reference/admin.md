---
description: Policy-controlled GitHub administration and audit records.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-13T14:19:54Z"
status: stable
tags: [administration]
title: GitHub administration
type: Reference
---

# GitHub administration

`tbhbdev admin` makes one REST call with a fresh installation token restricted to the endpoint's permissions and repository or organization. Connect supplies the private key through the service's reader. Callers receive the response body. They never receive credentials, including the private key or JSON Web Token (JWT). This command keeps credentials in memory without caching them.

```sh
tbhbdev admin call GET /repos/tbhb-dev/widget --reason 'inspect settings'
tbhbdev admin call PUT /repos/tbhb-dev/widget/environments/test --json request.json --reason 'create environment'
tbhbdev admin secret-repos add --org tbhb-dev --secret BUILD widget tbhb-dev/other
```

`call` requires `--reason`. Use `--json -` for standard input. `--dry-run` checks policy and writes an audit record without reading secrets or making requests. With `secret-repos add`, the command first checks for `selected` visibility. It then looks up repository IDs and adds them through the same call path. Repositories must belong to the specified organization. This subcommand defaults its reason to `add repositories to selected secret`. Use `--reason` to override it.

Both commands accept `--app` (default `tbhb-dev-admin`), `--app-id` (default `5733271`), `--vault` (default `tbhb-dev-admin`), and `--item` (default `github-app-tbhb-dev-admin`). Verify the item name before deployment. Only the item's `private_key` field comes from Connect. The App ID controls authentication, while the app name identifies the configuration.

## Key host

Because one host uses the App, one host reads its key. On that host, the file `enabled` exists in the audit directory (`~/.local/state/tbhb/admin-app/enabled`) as a regular file with the caller's user ID, never a symlink. Any other host fails the command with `credential_unavailable` before it contacts Connect, though policy refusals and dry runs still work there. Ops creates it.

This file records an opt-in and grants nothing, because any process can create a file in its own home. Access control comes from the Connect token for the vault that contains the key, which just the one host has. Install the tool there with `uv tool install git+https://github.com/tbhb-dev/tools` pinned to a merged commit, then create the file.

Audit records moved from `tbhbdev/admin` to `tbhb/admin-app` in the state directory. A host that logged to the old path starts a new chain.

## Policy and exits

The allowlist covers settings, protection, Actions secrets and variables, environments, self-hosted runners and runner groups, custom properties, contents, and pull requests. Unknown endpoints fail closed. The policy only passes the `tbhb-dev` owner, and it never approves or merges a pull request. Protection writes pass only when they activate a ruleset with `{"enforcement":"active"}` or enable required signatures or administrator enforcement. A `POST` to a repository's `rulesets` endpoint also passes when the body creates an active branch ruleset with an empty bypass actor list. Its rules must come from the live `main` ruleset: `deletion`, `non_fast_forward`, `required_linear_history`, `required_signatures`, `pull_request` with exactly one approval, squash merges only, no code owner review and no required reviewer team, and `required_status_checks` that name only `conclusion`, bound to no app or to the Actions app with `integration_id` 15368. A new ruleset adds restrictions and never removes one. Other protection writes fail closed because the tool can't classify them without previous state. Security analysis settings fail closed on all repositories. Settings updates accept explicit field lists.

Path normalization folds names to lower case to check the policy and decodes percent escapes once. The request sends the path with its original case, which keeps a contents path such as `README.md` intact. It drops query strings and strips trailing slashes before checking for routing ambiguity. GitHub receives the path alone. Encoded separators, repeated slashes, dot segments, fragments, and absolute URLs fail closed. The request uses that normalized path. Write parameters belong in JSON bodies. HTTP calls have a timeout and a 1 MiB response cap. Redirects fail closed.

Before an environment write to an existing environment, the tool reads that environment. Its reviewers must match the reviewers in the write. Its branch policy must be unset or `{"protected_branches": true, "custom_branch_policies": false}`. A positive wait timer refuses the write, because a write would reset the timer to 0. These refusals use the `protection_weakening` code. GitHub offers only unconditional writes between the read and the write. The tool doesn't see a concurrent change in that window and accepts that risk.

A refusal exits 3 with its code on standard error. Usage errors exit 2. HTTP error responses exit 1 with the response body.

| Code | Meaning |
| --- | --- |
| `protection_weakening` | Protection deletion, bypass actors, an unclassified protection write, or an environment write unless it's a `PUT` that lists reviewers with only `type` and `id` keys and omits `wait_timer`. The body must also state `can_admins_bypass: false` and `prevent_self_review: true`, plus the deployment branch policy `{"protected_branches": true, "custom_branch_policies": false}`. A repository `default_branch` change, `allow_auto_merge: true`, or `web_commit_signoff_required: false` also counts |
| `environment_unreadable` | An environment write where the lookup returned a status other than 200 or 404. Also a lookup whose JSON has a missing field or a malformed reviewer id or wait timer, or whose nesting is too deep. |
| `broad_visibility` | An organization secret, variable, or runner group whose visibility isn't `selected`, a new runner group that omits it, a runner group that allows public repositories, a runner group whose `restricted_to_workflows` isn't true, or any `selected_workflows` change |
| `membership_change` | Membership, invitations, collaborators, or role changes |
| `repository_removal` | Repository deletion, transfer, or archiving |
| `paid_feature` | Security analysis, larger runners, Codespaces, Copilot, billing, or spending |
| `public_visibility` | Public visibility, `private: false`, or a new organization repository that omits `private: true` or `visibility: private` |
| `approval_or_merge` | A write to a pull request's `reviews`, or any request to its `merge` endpoint |
| `foreign_owner` | A repository or organization path whose owner isn't `tbhb-dev` |
| `ambiguous_path` | Routing ambiguity or an invalid identifier |
| `invalid_method` | Method outside `GET`, `POST`, `PUT`, `PATCH`, `DELETE` |
| `invalid_body` | Unreadable JSON or a body other than an object |
| `endpoint_denied` | Unknown endpoint, GraphQL, or runner registration tokens |
| `unknown_setting` | Setting outside the field allowlist |
| `audit_unavailable` | Unsafe log or failure to append or sync |
| `credential_unavailable` | Connect didn't supply a nonempty key |
| `request_failed` | Authentication, transport, or repository selection failure |
| `redirect_refused` | HTTP redirect |
| `response_too_large` | Response exceeded 1 MiB |
| `invalid_response` | Metadata lacks an object or a positive repository ID |
| `secret_not_selected` | Secret visibility differs from `selected` |
| `repository_mismatch` | Invalid repository or another organization |
| `reason_required` | Empty reason |
| `invalid_setting` | Invalid credential setting |

## Append-only audit log

The log path is `$XDG_STATE_HOME/tbhb/admin-app/calls.jsonl`, defaulting to `~/.local/state/tbhb/admin-app/calls.jsonl`. Its directory must belong to the caller with mode `0700`. Each open requires a caller-owned regular file with mode `0600` and one hard link. Symlinks fail closed. Opens always use append, create, and no-follow flags. No command edits the log. Each write holds an exclusive lock from the size snapshot through its rollback. A failed write then cuts off only its own partial line and syncs the cut. If the last line is unfinished, the next writer ends it with a newline and leaves the fragment as a line that's not JSON, so readers must skip lines that aren't valid JSON. A writer refuses a log file that another process replaced between its open and its lock. Every call syncs the log directory and each of its ancestors.

Each JSON line contains `prev`, the Secure Hash Algorithm (SHA) 256 digest of the previous line (64 zeros for the first line), so a deleted or edited line breaks the chain from that point. Each line also contains `id`, `time` in Coordinated Universal Time (UTC), caller `uid`, `reason`, `method`, normalized `path`, `decision` (`allow` or `refuse`), `code`, HTTP `status` or null, `phase`, and `body_sha256`. The hash covers canonical JSON bytes sent to GitHub, or empty bytes for an absent body. Records never contain request bodies or credentials. Keep secrets out of reasons.

Before reading credentials or making HTTP requests, the command appends and syncs a `before` record. Refusals and dry runs need only that record. An attempted call adds an `after` record with the same ID and its status or failure code. A missing second record can mean an interrupted call. An audit failure stops the command, though a failure after the request can't undo an operation GitHub already accepted.
