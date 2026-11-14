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
    at: "2026-11-14T20:09:32Z"
status: stable
tags: [alpha]
title: Secret scan
type: Reference
---

# Secret scan

`tbhbdev alpha pr create` and `pr edit` scan the final title and body, trailers included, and `tbhbdev alpha merge` scans the squash message, with `gitleaks stdin --redact` before `gh` runs. The scan uses the host's private config, `.config/gitleaks/private.toml` in the home directory, which the host's provisioning tooling installs and which extends the repository's `.gitleaks.toml`. It fails closed. The command stops on a private config that's missing or unreadable, or that doesn't parse to its canary rule and at least one other rule. A missing `.gitleaks.toml`, a gitleaks failure, or any finding stops it too. Findings appear by rule ID and line only. A gitleaks failure or config error shows only gitleaks' exit status, never its output, which can quote a private rule's pattern. `pr check` and the draft hook don't scan.

The scan uses the visibility of the repository the text goes to, not the working directory's. `pr edit` and `merge` take that repository from the pull request's own `url` and read `gh repo view <owner/name>` once. `pr create` has no pull request yet, so it reads `gh repo view` once for the base repository, which is the one `gh pr create` resolves. A `url` that isn't `https://github.com/<owner>/<name>/pull/<number>`, or a base repository without a parseable `nameWithOwner`, is an unresolved target, and then edit and merge don't read the visibility. In a repository that `gh` reports as `PRIVATE`, the scan skips the private config's name rules: the rule IDs `private-repo`, `tailnet-host`, `tailnet-stem`, `tailnet-name`, `tailnet-suffix`, `local-name`, `home-path` and `tool-dir`, each with its trailing `-<digits>`. This matches the rules' gates, which apply the private config only to a repository that sets `--@tbhb_rules//lint:gitleaks_private_config`. The scan runs under a temporary copy of the private config without those rules, in a 0700 directory as a 0600 file, removed afterwards. Every other rule still applies: the `op-*` rules, `tailnet-ipv4`, `tailnet-ipv6`, `private-ipv4`, `mac-address`, `endpoint` and `keychain`, the canary, `.gitleaks.toml` and gitleaks' defaults. `PUBLIC`, `INTERNAL`, any other value, an empty value, a failed read or an unresolved target gets the full private config.

## Staged changes

`tbhbdev alpha install` writes pre-commit and pre-merge-commit hooks that run `tbhbdev alpha pre-commit-scan` last. Each hook first runs the chained hook: prek's own hook for the stage, installed as `<hook>.prek` when the prek config has hooks for it, or else a `<hook>.legacy`. The scan runs only when the chained hook passes, on the index that hook left, and its status is the hook's. That command runs `gitleaks git --pre-commit --staged --redact` with the same private config. Both the repository's `.gitleaks.toml` and private rules such as `tailnet-ipv4` apply to each line the commit adds, while unstaged changes and existing lines pass. gitleaks reads the index of the commit in progress, so `git commit -a`, `git commit PATH` and a merge commit scan what the commit records. The scan reads `.gitleaks.toml` and `.gitleaksignore` from that index too, so an unstaged edit to either can't loosen the scan. A merge commit's scan also covers the lines the merged branch adds. Without `.gitleaks.toml` in the index, the private config applies with gitleaks' default rules in place of the repository's file. These hooks are plain shims, not prek hooks, because prek stashes unstaged changes and fails when the work tree changes during the hook. Install moves someone's own hook for either stage to `<hook>.legacy` with a warning, and that hook still runs before the scan. If `prek install` fails, install still writes the shim that scans and reports the failure.

A commit hook doesn't call `gh`. The scan reads the visibility locally and skips the name rules in one case: origin is a GitHub repository of the tbhb-dev organization whose name is on `~/.local/state/tbhb/private-repos.txt`, the pre-push guard's list. An unreadable list, an origin that doesn't parse, another owner, no origin, or a name that only has a `-private` component gets the full private config.

Like the pull request scan, the commit scan fails closed. It refuses the commit on a private config that's missing, unreadable or lacks the canary and another rule, when git can't read the index, when neither PATH nor `mise which` finds gitleaks, on a gitleaks failure, and on any finding. In a tbhb-dev clone with the hooks installed, a host whose private config is missing or unreadable can't commit until its provisioning tooling restores the config, and a host without gitleaks can't commit until `mise install` or PATH supplies it. Without `tbhbdev` on PATH, no scan runs, and the shim refuses commits in a tbhb-dev repository until `tbhbdev` is back on PATH. A finding appears by rule ID, file and line only, such as `rule tailnet-ipv4-1 in README.md:2`. The refusal shows the file's path on purpose, so the committer can find the line. In a private repository the path can include private names, and agent transcripts record that stderr. Git's `--no-verify` skips this scan and the identity hooks. Standing rules prohibit it.
