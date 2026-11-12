---
description: Secret scanning for pull request and merge messages.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: Secret scan
type: Reference
---

# Secret scan

`tbhbdev alpha pr create` and `pr edit` scan the final title and body, trailers included, and `tbhbdev alpha merge` scans the squash message, with `gitleaks stdin --redact` before `gh` runs. The scan uses the host's private config, `.config/gitleaks/private.toml` in the home directory, which the host's provisioning tooling installs and which extends the repository's `.gitleaks.toml`. It fails closed. The command stops on a private config that's missing, unreadable or lacks the `tbhb-private-canary` rule. A missing `.gitleaks.toml`, a gitleaks failure, or any finding stops it too. Findings appear by rule ID and line only. `pr check` and the draft hook don't scan.

The scan uses the visibility of the repository the text goes to, not the working directory's. `pr edit` and `merge` take that repository from the pull request's own `url` and read `gh repo view <owner/name>` once. `pr create` has no pull request yet, so it reads `gh repo view` once for the base repository, which is the one `gh pr create` resolves. A `url` that isn't `https://github.com/<owner>/<name>/pull/<number>`, or a base repository without a parseable `nameWithOwner`, is an unresolved target, and then edit and merge don't read the visibility. In a repository that `gh` reports as `PRIVATE`, the scan skips the private config's name rules: the rule IDs `private-repo`, `tailnet-host`, `tailnet-stem`, `tailnet-name`, `tailnet-suffix`, `local-name`, `home-path` and `tool-dir`, each with its trailing `-<digits>`. This matches the rules' gates, which apply the private config only to a repository that sets `--@tbhb_rules//lint:gitleaks_private_config`. The scan runs under a temporary copy of the private config without those rules, in a 0700 directory as a 0600 file, removed afterwards. Every other rule still applies: the `op-*` rules, `tailnet-ipv4`, `tailnet-ipv6`, `private-ipv4`, `mac-address`, `endpoint` and `keychain`, the canary, `.gitleaks.toml` and gitleaks' defaults. `PUBLIC`, `INTERNAL`, any other value, an empty value, a failed read or an unresolved target gets the full private config.
