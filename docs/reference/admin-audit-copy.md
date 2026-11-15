---
description: The off-host copy of the administrator audit log and the gate on the App key.
generated:
  - role: generated
    harness: claude-code
    model: anthropic:claude-sonnet-5-5
    at: "2026-11-15T08:00:00Z"
status: stable
tags: [administration]
title: Administrator audit copy
type: Reference
---

# Administrator audit copy

The administrator command sends its audit log to a receiver on another host. It reads the App key only after the receiver acknowledges the call's `before` record. The receiver is `tbhbdev.admin.audit_receiver`, a standard-library module that an SSH forced command runs as `python -I audit_receiver.py DIR SEGMENT QUOTA`. This page names roles only. The operator keeps the receiver host and its account in private records.

## Receiver protocol

The forced command reads `SSH_ORIGINAL_COMMAND`, which is `head` or `append K`. Each request takes one exclusive lock.

- `head` replies with the receiver's count and the digest of its last line.
- `append K` takes one line on standard input. The line is bytes 32 through 126 and one final line feed. It fits in 65536 bytes including the line feed. It parses as one JSON object without duplicate keys, `NaN`, or `Infinity`. Its `prev` equals the head digest.
- A replay is a `K` at or below the count. The receiver checks the line's structure and byte range. When `K` equals the count, the line must also hash to the head digest. A mismatch exits 1 with empty output.
- The reply is `ack 1 N DIGEST` and a line feed. `DIGEST` is the Secure Hash Algorithm 256 digest, in hexadecimal, of the line without its line feed (64 zeros for an empty log). `N` is always the newest line, whatever `K` was.
- The quota applies to a fresh append. A head file records the segment, count, digest, and length. Recovery cuts the segment back to that length and rotates segments.

## Sender

Each request runs `ssh -F` with a configuration, a pinned host key file, and a target file in the audit directory. The command line forces `BatchMode=yes` and `StrictHostKeyChecking=yes`. It also sets an empty global known hosts file and turns off DNS host key lookup, the known hosts command, host key updates, the control path, and GSSAPI. A connection serves one request, and a failed request isn't retried.

The sender keeps the last acknowledged count and digest in a state file beside the log. It checks each acknowledgment against the local log. Count 0 needs the digest of 64 zeros. Any other count needs local line `N` to exist and hash to the digest. Failing either check refuses with `audit_copy_diverged`, and the cached count clears after a rejected append.

Lines go oldest first. Failing the structural checks triggers a `head` request, and a receiver count still below that line refuses the call with `audit_copy_damaged`. When `prev` differs from the previous local line, the call refuses with `audit_copy_diverged`. If the receiver refuses a replay because its newest line differs from the sender's, the call shows `audit_copy_unacknowledged`, and the next `head` request reveals the divergence.

## The gate

A key call appends its `before` record. It then sends the backlog through that record within one 10 second deadline, which includes the lock wait. The sender reads the key only after an acknowledgment at or past that record passes the check. Otherwise the call refuses with `audit_copy_unacknowledged`, and a refusal record goes into the local log. A host with the `enabled` marker but no receiver setting refuses with `credential_unavailable`.

Policy refusals and dry runs also append and try to send, and a failed send doesn't stop them. After a call, the `after` record goes into the log, shortened to 1024 bytes by cutting its `reason` and `path`, and the sender tries once to send it. A failed trailing send leaves the record for the next call or for `catch-up`. A `before` record over 64512 bytes refuses with `audit_record_too_large`, as does any line over 65536 bytes.

## Catching up

`tbhbdev admin catch-up` sends part of the backlog. It takes the lock for each request and never reads the key, and it leaves the call records alone. After 20 lines or 30 seconds it stops. It then prints the receiver's count and exits 0. Run it again until the count matches the local log. A failed request exits nonzero.

## Operator steps

These steps stay outside the tool. The operator installs the forced command on the receiver and writes the three setting files in the audit directory. Before enabling the marker, the operator runs `ssh -G` with the same options and confirms that the configuration resolves to the pinned key. A repair command on the receiver, which appends a line that skips the JSON check, belongs to a later change.
