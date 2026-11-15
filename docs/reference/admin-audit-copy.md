---
description: The off-host copy of the administrator audit log and the gate on the App key.
generated:
  - role: generated
    harness: claude
    model: anthropic:claude-sonnet-5-5
    at: "2026-11-15T08:00:00Z"
status: stable
tags: [administration]
title: Administrator audit copy
type: Reference
---

# Administrator audit copy

The administrator command sends its audit log to a receiver on another host, and reads the App key only after the receiver holds the call's `before` record. The receiver is `tbhbdev.admin.audit_receiver`, a standard-library module that an SSH forced command runs as `python -I audit_receiver.py DIR SEGMENT QUOTA`. This page names roles only. The operator keeps the receiver host and its account in private records.

## Receiver protocol

The forced command reads `SSH_ORIGINAL_COMMAND`, which is `head` or `append K`. Each request holds one exclusive lock.

- `head` replies with the receiver's count and the digest of its last line.
- `append K` carries one line on standard input. A line must hold bytes 32 through 126 followed by one line feed, stay within 65536 bytes including the line feed, and be one JSON object with no duplicate keys and no `NaN` or `Infinity`. Its `prev` must equal the head digest. A replay (`K` at or below the count) checks structure only.
- The reply is `ack 1 N DIGEST` and a line feed, where `DIGEST` is the SHA-256 of the line without its line feed (64 zeros for an empty log).
- The quota applies to a fresh append only. A head file records the segment, count, digest, and length. Recovery cuts the segment back to that length and rotates segments.

## Sender

The sender runs `ssh -F` with a configuration, a pinned host key file, and a target file in the audit directory. The command line forces `BatchMode=yes`, `StrictHostKeyChecking=yes`, an empty global known hosts file, no DNS host key lookup, no known hosts command, no host key updates, no control path, and no GSSAPI. It sends one request per connection, with no retries.

The sender keeps the last acknowledged count and digest in a state file beside the log. Every acknowledgment passes a check against the local log: a count of 0 needs the digest of 64 zeros, and any other count needs local line `N` to exist and hash to the digest. A failure refuses with `audit_copy_diverged`, and a rejected append clears the cached count.

Lines go oldest first. A line that fails the structural checks first triggers a `head` request. If the receiver's count has not reached the line, the call refuses with `audit_copy_damaged`. A `prev` that does not match the previous local line refuses with `audit_copy_diverged`.

## The gate

A key call appends its `before` record, then sends the backlog through that record within one 10 second deadline that includes the lock wait. The sender reads the key only after an acknowledgment at or past that record passes the check. Otherwise the call refuses with `audit_copy_unacknowledged`, and a refusal record goes into the local log. A host with the `enabled` marker but no receiver setting refuses with `credential_unavailable`.

Policy refusals and dry runs also append and try to send, and a failed send doesn't stop them. After a call, the `after` record goes into the log, shortened to 1024 bytes by cutting its `reason` and `path`, and the sender tries once to send it. A failed trailing send leaves the record for the next call or for `catch-up`. A `before` record over 64512 bytes, or any line over 65536 bytes, refuses with `audit_record_too_large`.

## Catching up

`tbhbdev admin catch-up` sends part of the backlog. It takes the lock for each request, never reads the key, writes no call record, and stops after 20 lines or 30 seconds. It then prints the receiver's count and exits 0. Run it again until it reports nothing left to send. A failed request exits nonzero.

## Operator steps

These steps stay outside the tool. The operator installs the forced command on the receiver, writes the three setting files in the audit directory, and runs `ssh -G` with the same options to check that the configuration resolves to the pinned key before enabling the marker. A repair command on the receiver, which appends a line that skips the JSON check, belongs to a later change.
