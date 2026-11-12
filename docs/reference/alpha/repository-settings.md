---
description: Standard repository settings and bootstrap behavior.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: Repository settings
type: Reference
---

# Repository settings

`tbhbdev alpha repo bootstrap [--dry-run] [--create (--private|--public)] ORG/REPO` brings a repository to the standard settings. It's idempotent: when a repository already matches, the command prints `up to date` and stops.

The settings aren't in this package. An installed plugin supplies the config file `repo-settings.toml`. The settings change only by a pull request in that plugin's repository. When no plugin supplies the file, the command fails before it calls GitHub, and the message says which file is missing. The config must have a `[defaults]` table. A `[repos."owner/name"]` table adds to the defaults for one repository, and its keys win. Repository names match without regard to case. A repository name may begin with a dot, as in `example-org/.github`. The names `.` and `..` fail, and so does an owner that begins with a dot. The command rejects any other character than letters, digits, `.`, `_` and `-`, in the command-line argument and in a config key alike.

```toml
[defaults]
allow_squash_merge = true
allow_merge_commit = false
allow_rebase_merge = false
squash_merge_commit_title = "PR_TITLE"
squash_merge_commit_message = "BLANK"
delete_branch_on_merge = true
has_issues = false
has_wiki = false
has_projects = false

[repos."example-org/example-repo"]
has_issues = true
```

Each key is a field of GitHub's update-a-repository endpoint, and only these keys are valid: `allow_squash_merge`, `allow_merge_commit`, `allow_rebase_merge`, `squash_merge_commit_title` (`PR_TITLE` or `COMMIT_OR_PR_TITLE`), `squash_merge_commit_message` (`PR_BODY`, `COMMIT_MESSAGES`, or `BLANK`), `delete_branch_on_merge`, `has_issues`, `has_wiki`, `has_projects`, `allow_auto_merge`, `allow_update_branch`, and `web_commit_signoff_required`. All but the two commit-message keys take `true` or `false`. Any other key fails validation with a message that gives the file and the key. That includes `visibility`, `private`, `archived`, `default_branch`, `name`, and the security settings. A config can't rename, archive, or expose a repository.

The command runs `gh api` with `GH_CONFIG_DIR` set to `.config/gh-agent` in the home directory. It removes `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, and `GH_HOST` from the environment it gives `gh`, because `gh` prefers a token variable to its config. The agent's `gh` config is the only credential, and a missing login fails with a `gh` error instead of falling back to another one. It reads `GET /repos/{owner}/{repo}` and sends one `PATCH` of the keys that differ. Then it reads again. If a key still differs after the update, the command exits 1 and reports the key. GitHub ignores some fields on some plans and for some visibilities. When a squash commit-message key changes, the `PATCH` includes both of them, because GitHub requires the title with the message. When the config sets only one of them, the other goes in with the repository's current value. The verifying read compares only the keys the config sets. Rulesets are out of scope. The command never calls a ruleset, branch protection, collaborator, team, or webhook endpoint. Its only calls are `GET` and `PATCH` on `/repos/{owner}/{repo}` and `POST` on `/orgs/{org}/repos`.

- `--dry-run` reads only. It prints a `key: before -> after` line for each key that would change, then a count.
- `--create` needs exactly one of `--private` and `--public`. For an existing repository, a visibility that differs from the flag exits 1 before any write, and a matching one goes on to the settings. A missing repository gets `POST /orgs/{org}/repos` and then the settings. Without `--create`, it exits 1.
- Every run that changes something appends one JSON line to `$XDG_STATE_HOME/tbhbdev/repo-bootstrap.jsonl`, which defaults to `~/.local/state/tbhbdev/repo-bootstrap.jsonl`. The directory has mode 0700 and the file 0600. A line has only the fields `v` (always 1), `ts` (the time, such as `2026-11-09T13:30:05Z`), `repo`, `created`, `changed` (each key with its `[before, after]` values), and `verified`. Dry runs and up-to-date runs leave the log alone. The command also sets those modes on an existing log file and directory, not only on new ones. A repository the command created gets a line even when a later step fails: `created` is `true` and `verified` is `false`, and `changed` is empty when the failure came on the first read after the create. If a `gh` call fails and the log write fails too, the command reports the `gh` error. If the log write fails after a successful `PATCH`, the command exits 1 after the change went through.

Exit status 0 covers a finished update, an up-to-date repository, and a dry run. Exit status 1 covers a config, validation, `gh`, or verification failure, with one line on stderr. Exit status 2 covers a usage error.
