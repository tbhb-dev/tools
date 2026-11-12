"""`tbhbdev alpha guard-approve`: require the exact approval command form.

A Claude Code Bash PreToolUse hook. Any command text that mentions
``tbhb-approve`` must be exactly one simple command:

    ~/.local/bin/tbhb-approve <repo> <pr> <sha>

The program word is literal, with an unquoted tilde. The repository is a bare
name, the pull request number has no leading zero, and the required commit
hash is 40 lowercase hexadecimal characters. Leading and trailing whitespace
is fine. Shell syntax, quoted words and additional commands are refused.

Refusing even ``grep tbhb-approve notes.md`` is deliberate: the operator's
rule is "mentions". A false refusal costs one rewrite while a stall costs
an hour. Commands without that text pass untouched. Like the other guards,
malformed payloads and unreadable input exit successfully without output.
"""

import json
import re
from contextlib import suppress

from tbhbdev.alpha.guard_gh_pr import Scanner, command_of, decision
from tbhbdev.alpha.repo import read_hook_stdin

BARE = re.compile(r"[A-Za-z0-9._~/ \t-]+")
FORM = re.compile(
    r"~/\.local/bin/tbhb-approve [A-Za-z0-9._-]+ [1-9][0-9]* [0-9a-f]{40}"
)
REASON = (
    "Use exactly ~/.local/bin/tbhb-approve <repo> <pr> <sha>. "
    "Run it alone as one command."
)


def denied(text: str) -> bool:
    """Whether command text mentions approval without using the exact form."""
    if "tbhb-approve" not in text:
        return False
    text = text.strip()
    if not BARE.fullmatch(text):
        return True
    words = Scanner(text).run()[0]
    return FORM.fullmatch(" ".join(words)) is None


def main() -> int:
    """Read a hook payload and print a deny decision for a refused command."""
    with suppress(Exception):
        text, antigravity = command_of(read_hook_stdin())
        if denied(text):
            print(json.dumps(decision(REASON, antigravity=antigravity)))
    return 0
