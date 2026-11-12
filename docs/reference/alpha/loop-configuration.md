---
description: Loop settings, shadow rules, and store behavior.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: Loop configuration
type: Reference
---

# Loop configuration

`tbhbdev alpha loop` reads the optional `[loop]` table from the user config. Calling this command requires the settings shown below. An unreadable or invalid config file fails with its path. A missing table or key, an unknown key, or an invalid setting fails with the path and `[loop] key`. `--help` works without config.

```toml
[loop]
repositories = ["example-org/example-repo"]
approval_command = ["example-approve", "--quiet"]
store_path = "/example/state/loop.sqlite3"
reconcile_interval_minutes = 5
spawn_cap_total = 4
spawn_cap_per_host = 2
daily_spawn_cap = 20
hosts = ["example-host-a", "example-host-b"]
allow_claude_retry = false

[loop.rule_modes."example-org/example-repo"]
R0 = "shadow"
R1 = "shadow"
R2 = "shadow"
R4 = "shadow"
R9 = "shadow"
```

The settings have these constraints:

- `repositories` lists unique `owner/name` strings.
- `approval_command` lists the approval program followed by its arguments, preserving arguments with spaces. The loop doesn't store a token.
- `hosts` lists unique host strings. These three lists must contain at least one entry, and entries must be non-blank strings.
- `store_path` is a non-blank path string without a null byte. After expanding `~`, it must be absolute.
- The interval and caps are positive integers. Booleans and fractional values fail. The spawn caps cover the total and each host. `spawn_cap_per_host` must not exceed `spawn_cap_total`. The daily cap covers spawns per day.

`allow_claude_retry` is an optional boolean, defaulting to false. R8 retries on Codex unless this flag permits a timed-out or unknown Codex run to retry on Claude. It still needs configured host and harness capacity. `rule_modes` is an optional table of configured repositories, each containing R0 through R11 with values `off`, `shadow`, or `on`. Unlisted rules are off. The runner rejects every `on` mode with `LiveModeError` at startup. Live execution isn't implemented.

Even with valid settings, the command exits with status 1 and reports that the shadow runner exists but live intake isn't wired yet. It doesn't create a store or execute an approval or other action. The Python runner accepts `EventSource`, `ReconcileReader`, `PeerAgent`, `GitHubWriter`, and `Approval` protocols, with fakes in the tests. Intake supplies validated receipts and immutable observations from each delivered event and verified context. Reconcile reads supply fresh facts and typed evidence of completed effects.

`Runner.run` reconciles before consuming deliveries. R0 gates reconcile reads by rule mode, schedule, and quota reserve. The runner records each intent with state `shadow` and appends a JSON line with the receipt or sweep identifier, derived state, rule, key, kind, and outcome. Replayed deliveries and restarts use the same keys, while fresh observations supply PR state. R3 has no action key: each processed delivery or sweep logs the observed decline again, including repeated sweep identifiers. These lines describe observations and don't count as scored intents. The runner never calls an effect interface.

Each sweep compares prior intents with head-bound approvals, merges, reviewer runs, and typed effect observations. In Phase 0, R0 agrees on a sweep. Only R1, R2, R4, and R5 can reach `agree` from PR facts. R6 through R11 need explicit effect evidence to agree. A PR absent from the snapshot or superseded by a new head scores `no_change`. The runner rejects invalid effect results before writing. `score_shadow` records `agree`, `disagree`, or `no_change` in an immediate transaction and skips rows whose timestamps are ahead of the sweep. For each skipped non-final row, the runner logs `source` (the sweep key), `rule`, `action_key`, and `outcome: "skipped_future"` as one JSON line, while final comparisons remain immutable and don't generate skip logs. A later sweep can replace `no_change` with a final score.

`Runner.report()` reads one store snapshot and returns counts by rule and repository, plus the count of R9 items it would have written. Twenty final scores make a rule eligible for promotion review. Provisional `no_change` scores don't count. Each rule summary has `needs_review` set when any disagreement needs explanation before promotion.

Eligibility alone never permits promotion. Phase 0 rejects live modes regardless of score.

The store API takes the configured `store_path` with no default. `Store(path)` opens an existing database. Use `OpenMode.CREATE` for a new one. One connection uses write-ahead logging, full synchronization, a five-second busy timeout, and owner-only file permissions. The parent directory must be private and must not be a symlink. A sibling `<store_path>.lock` file excludes a second opener for the store's lifetime, including during a rebuild. `:memory:` supports isolated tests. Only the runner writes through this API. No table stores pull request state.

`record_intent(key, time)` claims a new key and returns false when a live key already exists. A live intent replaces an existing shadow row. Promotion resets its timestamps and result. `existing_keys()` excludes shadow rows. `shadow_keys()` supplies them separately. An action advances from `intent` to `started`, then `done` or `failed`. The store serializes transition reads and writes in one transaction. Invalid transitions and state/result pairs raise `TransitionError`. Done actions require `ok`. Failed actions require `error`, `timeout`, or `refused`. A shadow row can persist a runner-supplied `agree`, `disagree`, or `no_change` result without becoming live. The store doesn't compute shadow scores.

Receipts and identifiers reject values outside their patterns. Duplicate delivery ids return `duplicate` without replacing the first receipt: this means seen, not processed. `crashed_intents()` supplies keys that have no start. `unfinished_starts()` supplies started keys without a terminal result. Both need recovery reads before a retry. `record_escalation()` returns the marker and whether it created the row. `stopped_objects()` derives the rules' stopped objects from live escalation and stop keys.

`counts(object_key)` returns failure totals by key family across heads. Families keep the action kind, object, and any step or reason. Review totals include every verdict. The runner combines them with approvals from reads to fill the rules' `reviewer_runs_without_approval` input. Retry counts use recorded runs under `retry:<run>`. This count is a presence flag because action keys are unique. Shadow keys contribute neither failures nor run counts. The counts and existing keys feed the rules without changing their input types.

`StoreError` reports a missing, corrupt, or locked database. Schema version 3 adds shadow intent dimensions for reports. A mismatched version raises `SchemaVersionError` with code `SCHEMA_VERSION`, naming the found version and requiring a rebuild before any journal settings change. To rebuild it, stop the loop and open the store with `OpenMode.REBUILD`. Rebuilding discards the database and its sidecars. The lock remains acquired throughout the rebuild. `Recovery.KEYS_LOST` tells the runner that it no longer has previous action keys. The runner must reconcile before taking more work. Database errors fail the operation and close unreadable or damaged connections before another operation can use them.

The pure rules table requires a spawned reviewer's approval at the current green head and a complete diff read. Mechanical approval is undecided and defaults off in `RepositoryPolicy.mechanical_approval`. An explicit opt-in permits only an agent-opened PR with an exact configured template match. PRs opened by the loop process remain excluded. R1 skips loop-origin PRs that the loop can't approve. The caller must recompute `mechanical_template_matches`, `verification_passed`, and all other head-bound observations for the current head. Until the caller verifies the base repository, the rules skip the PR, including escalation. Pending signatures and pending CI have no rule or age timeout in this slice. R8 uses `stop:<run>` when capacity can't supply a retry target and reserves `retry:<run>` for a retry with a target. Escalation emits at most one intent per object per call and skips stopped objects.
