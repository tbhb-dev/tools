# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-gh-config-dir`: require GH_CONFIG_DIR for gh calls.

This PreToolUse hook shares guard-gh-pr's shell scanner and payload contracts.
It inspects the command string without running gh, and requires only a nonempty
GH_CONFIG_DIR in the hook's environment. Like the other guards, allowing prints
nothing and unreadable input or a crash fails open.
"""

import json
import os

from tbhbdev.alpha.guard_gh_pr import (
    command_of,
    decision,
    program_of,
    simple_commands,
)
from tbhbdev.alpha.repo import read_hook_stdin

REASON = "gh is blocked: set GH_CONFIG_DIR to a nonempty value before running gh."


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        text, antigravity = command_of(read_hook_stdin())
        if not os.environ.get("GH_CONFIG_DIR") and any(
            program_of(words) == "gh" for _, words in simple_commands(text)
        ):
            print(json.dumps(decision(REASON, antigravity=antigravity)))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
