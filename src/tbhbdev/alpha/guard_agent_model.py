# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-agent-model`: make every subagent launch name a model.

A PreToolUse hook for the ``Agent`` tool. A subagent launched without ``model``
inherits the session's model, and a ``fork`` always does, so a session on a
costly model launches costly subagents by accident. The guard refuses a launch
whose ``tool_input.model`` is missing or empty, and every ``fork``, by exiting 2
with the reason on stderr, which Claude Code feeds back to the model. Any other
tool, and input it can't read, is allowed.
"""

import sys

from tbhbdev.alpha.repo import read_hook_stdin

TOOL = "Agent"
FORK = "fork"
CHOICE = "Pick sonnet for most work, and opus or fable only for planning and design."
REASON_MODEL = f"Agent launch refused: add `model` to the call. {CHOICE}"
REASON_FORK = (
    "Agent launch refused: a fork always inherits the session's model. Launch "
    f"a subagent with an explicit `model` instead. {CHOICE}"
)


def verdict(data: dict) -> str | None:
    """The refusal reason for a hook payload, or None to allow it."""
    if data.get("tool_name") != TOOL:
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool_input.get("subagent_type") == FORK:
        return REASON_FORK
    model = tool_input.get("model")
    if not isinstance(model, str) or not model.strip():
        return REASON_MODEL
    return None


def main() -> int:
    """Read the payload on stdin; exit 2 with the reason for a refused launch."""
    reason = verdict(read_hook_stdin())
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2
