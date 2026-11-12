---
description: Database migrations, catalog checks, connection settings, and server tests.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [analytics]
title: Analytics
type: Reference
---

# Analytics

`tbhbdev analytics migrate` connects as the owner, serializes migrations with an advisory lock and records each version in its migration transaction. `--dry-run` lists pending migrations without changing the database. Ops must create an empty database and its owner login with `CREATEROLE` before migration.

`tbhbdev analytics contract check` separately compares the database catalog with the packaged contract and exits 1 on a difference. `migrate` alone doesn't check the contract. A deploy runs `tbhbdev analytics migrate` and then `tbhbdev analytics contract check`. A configuration mistake prints its message, such as `[analytics] password_file is required`. That message contains key names and never a value. Other failures print only redacted diagnostics.

Configure the connection through `[analytics]` in the user config. Set `password_file` to the owner password file and `loader_enabled` to `true` to enable loader access. The switch defaults to `false`.

The optional keys `reader_password_file`, `grafana_password_file`, and `loader_password_file` each point at one role's password file. `migrate` sets the password of a role that has a file. It creates a role that has no file with no password and leaves an existing role's password unchanged. It prints a line such as `role analytics_reader has no password (reader_password_file is not configured)` for each such role, with `--dry-run` too.

The `PGHOST`, `PGPORT`, `PGDATABASE`, and `PGUSER` environment variables override the matching connection settings. Analytics defaults to `localhost`, port 5432, database `analytics`, and user `analytics_owner`. These defaults form an exception to the deployment settings described in Config. The user must equal `owner_role`. Each password file must contain 32 characters drawn from `A-Z`, `a-z`, and `0-9` on its first line. The owner password also seeds the salt of every role password. Role settings `owner_role`, `loader_role`, `reader_role`, and `grafana_role` override the standard identifiers, and no two of them may be equal. Populate these files from 1Password and keep them under operator control.

Default tests use fake connections. The opt-in server test requires an empty disposable PostgreSQL 18 database. It reads the server from `ANALYTICS_TEST_HOST`, the database from `ANALYTICS_TEST_DATABASE`, and the owner password file from `ANALYTICS_TEST_PASSWORD_FILE`, and it skips when any is unset. It connects to that one database and ignores the `[analytics]` config and the `PG*` variables. Run `ANALYTICS_TEST_HOST=localhost ANALYTICS_TEST_DATABASE=analytics_test ANALYTICS_TEST_PASSWORD_FILE=owner_password uv run pytest tests/analytics_server.py -m analytics_server` to check lock contention, rollback, and an unchanged rerun. The test changes that database and leaves its schema for inspection.

Bazel never selects the server test.
