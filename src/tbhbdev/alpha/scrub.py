# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Redact credentials from text before a hook writes it to disk.

`scrub` replaces what looks like a credential with `[redacted]`: private key
blocks, GitHub, API, 1Password, AWS and Slack tokens, JWTs, bearer
credentials, passwords in URLs, and the value of an assignment whose name
holds `token`, `secret`, `password`, `api_key` or `credential`. It matches
shapes, so it can miss a credential with no recognizable shape and can redact
harmless text that looks like one. It never keeps a match.
"""

import re

PLACEHOLDER = "[redacted]"

# A pattern's group 1, when it has one, is the part kept in front of the mark.
PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bops_[A-Za-z0-9+/=_-]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"(://[^\s/:@]+:)[^\s/@]+(?=@)"),
    re.compile(
        r"""\b([A-Za-z0-9_]*(?:token|secret|password|passwd|api_?key|credential)"""
        r"""[A-Za-z0-9_]*\s*[=:]\s*)(?:"[^"\n]{8,}"|'[^'\n]{8,}'|[^\s"']{8,})""",
        re.IGNORECASE,
    ),
)


def keep_prefix(match: re.Match[str]) -> str:
    """The text that replaces MATCH: its group 1, if any, then the mark."""
    return (match.group(1) if match.lastindex else "") + PLACEHOLDER


def scrub(text: str) -> str:
    """TEXT with every credential-like span replaced by `[redacted]`."""
    for pattern in PATTERNS:
        text = pattern.sub(keep_prefix, text)
    return text
