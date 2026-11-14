---
description: Secret scanning for staged changes, pull request text and merge messages.
generated:
  - role: generated
    harness: claude-code:2.1.322
    model: anthropic:claude-opus-5-5
    effort: medium
    surface: cli
    session: sha-256-64:24ed779d56b187de
    subagent: sha-256-64:16dc27c13810e8fc
    at: "2026-11-14T19:19:10Z"
status: stable
tags: [alpha]
title: Secret scan
type: Reference
---

# Secret scan

`tbhbdev alpha pr create` and `pr edit` scan the final title and body, trailers included, and `tbhbdev alpha merge` scans the squash message, with `gitleaks stdin --redact` before `gh` runs. The scan uses the host's private config, `.config/gitleaks/private.toml` in the home directory, which the host's provisioning tooling installs and which extends the repository's `.gitleaks.toml`. It fails closed. The command stops on a private config that's missing, unreadable or lacks the `tbhb-private-canary` rule. A missing `.gitleaks.toml`, a gitleaks failure, or any finding stops it too. Findings appear by rule ID and line only. `pr check` and the draft hook don't scan.

The scan uses the visibility of the repository the text goes to, not the working directory's. `pr edit` and `merge` take that repository from the pull request's own `url` and read `gh repo view <owner/name>` once. `pr create` has no pull request yet, so it reads `gh repo view` once for the base repository, which is the one `gh pr create` resolves. A `url` that isn't `https://github.com/<owner>/<name>/pull/<number>`, or a base repository without a parseable `nameWithOwner`, is an unresolved target, and then edit and merge don't read the visibility. In a repository that `gh` reports as `PRIVATE`, the scan skips the private config's name rules: the rule IDs `private-repo`, `tailnet-host`, `tailnet-stem`, `tailnet-name`, `tailnet-suffix`, `local-name`, `home-path` and `tool-dir`, each with its trailing `-<digits>`. This matches the rules' gates, which apply the private config only to a repository that sets `--@tbhb_rules//lint:gitleaks_private_config`. The scan runs under a temporary copy of the private config without those rules, in a 0700 directory as a 0600 file, removed afterwards. Every other rule still applies: the `op-*` rules, `tailnet-ipv4`, `tailnet-ipv6`, `private-ipv4`, `mac-address`, `endpoint` and `keychain`, the canary, `.gitleaks.toml` and gitleaks' defaults. `PUBLIC`, `INTERNAL`, any other value, an empty value, a failed read or an unresolved target gets the full private config.

## Staged changes

The pre-commit hook that `tbhbdev alpha install` writes runs `tbhbdev alpha pre-commit-scan` before prek's own pre-commit hooks. That command runs `gitleaks git --pre-commit --staged --redact` with the same private config, from the top of the work tree. Both the repository's `.gitleaks.toml` and private rules such as `tailnet-ipv4` apply to each line the commit adds, while unstaged changes and existing lines pass. gitleaks reads the index of the commit in progress, so `git commit -a` and `git commit PATH` scan what the commit records. This hook is a plain shim, not a prek hook, because prek stashes unstaged changes and fails when the work tree changes during the hook. Install moves someone's own pre-commit hook to `pre-commit.legacy` with a warning, and prek's chained hook runs it after the scan.

A commit hook doesn't call `gh`. Instead, origin's repository name decides the visibility, checked against the local lists of the pre-push guard. A name with a `-private` component counts as `PRIVATE`, as does a name in `[pr_check] private_repos` or `~/.local/state/tbhb/private-repos.txt`, and then the scan skips the name rules. Any other origin, no origin, or an unreadable host list gets the full private config.

Like the pull request scan, the commit scan fails closed. It refuses the commit on a private config that's missing, unreadable or lacks the canary, on a missing `.gitleaks.toml`, when neither PATH nor `mise which` finds gitleaks, on a gitleaks failure, and on any finding. On a host without the private config, nobody can commit in a tbhb-dev clone with the hooks installed. A finding appears by rule ID, file and line only, such as `rule tailnet-ipv4-1 in README.md:2`. Git's `--no-verify` skips this scan and the identity hooks. Standing rules prohibit it.
