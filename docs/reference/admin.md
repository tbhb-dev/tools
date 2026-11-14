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

## Policy and exits

The allowlist covers settings, protection, Actions secrets and variables, environments, self-hosted runners and runner groups, webhooks, custom properties, contents, and pull requests. Unknown endpoints fail closed. The policy only passes the `tbhb-dev` owner, and it never approves or merges a pull request. Protection writes pass only when they activate a ruleset with `{"enforcement":"active"}` or enable required signatures or administrator enforcement. Other protection writes fail closed because the tool can't classify them without previous state. Security analysis settings fail closed on all repositories. Settings updates accept explicit field lists.

Path normalization lowercases names to check the policy and decodes percent escapes once. The request sends the path with its original case, so a contents path such as `README.md` keeps it. It drops query strings, so a query never reaches GitHub, and strips trailing slashes before checking for routing ambiguity. Encoded separators, repeated slashes, dot segments, fragments, and absolute URLs fail closed. The request uses that normalized path. Write parameters belong in JSON bodies. HTTP calls have a timeout and a 1 MiB response cap. Redirects fail closed.

A refusal exits 3 with its code on standard error. Usage errors exit 2. HTTP error responses exit 1 with the response body.

| Code | Meaning |
| --- | --- |
| `protection_weakening` | Protection deletion, bypass actors, an unclassified protection write, or an environment delete or change that drops reviewers, the wait timer, a deployment branch policy, or self-review prevention |
| `broad_visibility` | An organization secret or variable whose visibility isn't `selected` |
| `membership_change` | Membership, invitations, collaborators, or role changes |
| `repository_removal` | Repository deletion, transfer, or archiving |
| `paid_feature` | Security analysis, larger runners, Codespaces, Copilot, billing, or spending |
| `public_visibility` | Public visibility or `private: false` |
| `approval_or_merge` | A `POST` to a pull request's `reviews`, or any request to its `merge` endpoint |
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

The log path is `$XDG_STATE_HOME/tbhbdev/admin/calls.jsonl`, defaulting to `~/.local/state/tbhbdev/admin/calls.jsonl`. Its directory must belong to the caller with mode `0700`. Each open requires a caller-owned regular file with mode `0600` and one hard link. Symlinks fail closed. Opens always use append, create, and no-follow flags. No command edits or truncates the log.

Each JSON line contains `id`, `time` in Coordinated Universal Time (UTC), caller `uid`, `reason`, `method`, normalized `path`, `decision` (`allow` or `refuse`), `code`, HTTP `status` or null, `phase`, and `body_sha256`. The hash covers canonical JSON bytes sent to GitHub, or empty bytes for an absent body. Records never contain request bodies or credentials. Keep secrets out of reasons.

Before reading credentials or making HTTP requests, the command appends and syncs a `before` record. Refusals and dry runs need only that record. An attempted call adds an `after` record with the same ID and its status or failure code. A missing second record can mean an interrupted call. An audit failure stops the command, though a failure after the request can't undo an operation GitHub already accepted.
