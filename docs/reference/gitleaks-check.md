---
description: Fail-closed gitleaks scan of text before it is posted.
generated:
  - role: generated
    harness: claude-code
    model: anthropic:claude-sonnet-5-5
    at: "2026-11-22T00:00:00Z"
status: stable
tags: [secrets]
title: Gitleaks check
type: Reference
---

# Gitleaks check

`tbhbdev.gitleaks_check` scans text for secrets before a tool posts it. It's a thin public wrapper over the [secret scan](alpha/secret-scan.md) of `tbhbdev alpha pr` and `tbhbdev alpha merge`, which describes the private config, the canary rule and the visibility rules.

```python
scan_text(text: str, what: str, *, root: Path, gitleaks: str, visibility: str) -> None
```

- `text` is the text to scan and `what` names it in the refusal message.
- `root` is the repository root, which holds `.gitleaks.toml`.
- `gitleaks` is the gitleaks executable.
- `visibility` is the GitHub visibility of the repository the text goes to. Only `PRIVATE` skips the private config's name rules. Any other value, an empty one included, scans under the full private config.

The function returns `None` when the text is clean. It fails closed: it raises `ScanRefused`, a `RuntimeError`, on a missing or unreadable private config, one without the `tbhb-private-canary` rule, a missing `.gitleaks.toml`, a gitleaks failure, a missing or non-executable scanner and any finding. The message names rule IDs and line numbers, or for a scanner that can't run the executable and the OS reason, never matched text. The wrapper calls `tbhbdev.alpha.secret_scan.scan` and turns its `SystemExit` into `ScanRefused` with the same message, and an `OSError` from running the scanner into `ScanRefused`. `tbhbdev alpha pr` and `tbhbdev alpha merge` keep calling the scan directly.
