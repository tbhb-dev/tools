"""Verbatim sentence judge from origin/main 714fef3b6a8fbc2286a9542dd848898b9f4d1a52."""

import re

# The frozen baseline deliberately preserves these expressions verbatim.
# pylint: disable=duplicate-code
CLAIM = re.compile(
    r"\b(?:is|are|was|were|now|already|has been|have been)\s+"
    r"(?:done|applied|merged|shipped|deployed|live|fixed|complete|completed|"
    r"installed|running|verified|in place|started)\b"
    r"|\b(?:shipped|merged|applied|deployed|landed(?!\s+on\b)|rolled out)\b",
    re.IGNORECASE,
)

EVIDENCE = re.compile(
    r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b|#\d+|\bsays\b|\bsaid\b|\breports?\b|\.log\b|`[^`]+`"
    r"|\boutput\b|\bread-back\b|\bunverified\b|\bnot (?:done|applied|merged)\b",
    re.IGNORECASE,
)

# pylint: enable=duplicate-code
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n")

MAX_QUOTED = 160


def unsupported_claims(text: str) -> list[str]:
    """The sentences or lines of TEXT with a status claim and no evidence."""
    return [
        chunk.strip()[:MAX_QUOTED]
        for chunk in SENTENCE_END.split(text)
        if CLAIM.search(chunk) and not EVIDENCE.search(chunk)
    ]
