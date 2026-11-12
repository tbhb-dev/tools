# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-gh-config-dir`: keep gh calls on the agent's config dir.

This PreToolUse hook shares guard-gh-pr's shell scanner and payload contracts.
It inspects the command string without running gh. It denies a gh call when the
hook's own GH_CONFIG_DIR is empty or unset, and it denies any command that sets,
unsets or blanks GH_CONFIG_DIR to a value other than the hook's own, which
includes the operator's own gh directory. Like the other guards, allowing prints
nothing and unreadable input or a crash fails open.

It also scans assignment-only commands and `+=`, such as `GH_CONFIG_DIR=x; gh`.

Known gaps: `timeout N gh` and other runners, a program name held in a variable
(`x=gh; $x`), `read`, `printf -v`, `set -a`, `exec -c` and `sudo` forms, and
failing closed on a crash. Follow-up changes cover them.
"""

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard_gh_pr import (
    KEYWORDS,
    SHELLS,
    Scanner,
    command_of,
    decision,
    program_of,
    shell_script,
    simple_commands,
    skip_prefix,
)
from tbhbdev.alpha.repo import read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Iterator

VARIABLE = "GH_CONFIG_DIR"
REASON = "gh is blocked: set GH_CONFIG_DIR to a nonempty value before running gh."
REASON_OVERRIDE = (
    "The command is blocked: it sets, unsets or blanks GH_CONFIG_DIR. Leave the "
    "variable as the session set it."
)
# Builtins that set or remove a variable named in their arguments.
MUTATORS = {"export", "unset", "declare", "typeset", "readonly", "local"}
CLEARS_ENVIRONMENT = {"-i", "--ignore-environment", "-"}
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*\+?=")


def resolved(path: str) -> str:
    """``path`` with a leading ``~``, ``$HOME`` or ``${HOME}`` expanded, no slash."""
    home = str(Path.home())
    for prefix in ("~", "$HOME", "${HOME}"):
        if path == prefix or path.startswith(prefix + "/"):
            path = home + path[len(prefix) :]
    return path.rstrip("/")


def allowed_value(value: str, own: str) -> bool:
    """Whether ``value`` names the hook's own config dir."""
    return value in {f"${VARIABLE}", f"${{{VARIABLE}}}"} or (
        bool(value) and resolved(value) == resolved(own)
    )


def is_mutation(word: str, own: str) -> bool:
    """Whether a word sets, unsets or blanks GH_CONFIG_DIR to a foreign value."""
    name, equals, value = word.partition("=")
    if name == "--unset":
        return value == VARIABLE
    if name == f"{VARIABLE}+":
        return True
    if name != VARIABLE:
        return False
    return not equals or not allowed_value(value, own)


def raw_commands(text: str) -> Iterator[list[str]]:
    """Every command's words, keeping assignment-only commands.

    ``bash -c SCRIPT`` and ``eval`` are scanned in their place.
    """
    for command in Scanner(text).run():
        yield command
        words = skip_prefix(command)
        program = program_of(words) if words else ""
        if program in SHELLS:
            script = shell_script(words[1:])
            yield from raw_commands(script) if script is not None else iter(())
        elif program == "eval":
            yield from raw_commands(" ".join(words[1:]))


def leading_assignments(command: list[str]) -> list[str]:
    """The ``NAME=value`` and ``NAME+=value`` words that start a command."""
    found: list[str] = []
    for word in command:
        if ASSIGNMENT.match(word):
            found.append(word)
        elif word not in KEYWORDS:
            break
    return found


def overrides_variable(text: str, own: str) -> bool:
    """Whether a command in ``text`` changes GH_CONFIG_DIR away from ``own``."""
    if any(
        is_mutation(word, own)
        for command in raw_commands(text)
        for word in leading_assignments(command)
    ):
        return True
    for prefix, words in simple_commands(text):
        wrapped = any(program_of([word]) == "env" for word in prefix)
        if wrapped and CLEARS_ENVIRONMENT.intersection(prefix):
            return True
        scanned = prefix + (words if wrapped or program_of(words) in MUTATORS else [])
        if any(is_mutation(word, own) for word in scanned):
            return True
    return False


def verdict(text: str) -> str | None:
    """The deny reason for a command, or None to allow it."""
    own = os.environ.get(VARIABLE, "")
    if overrides_variable(text, own):
        return REASON_OVERRIDE
    if not own and any(program_of(words) == "gh" for _, words in simple_commands(text)):
        return REASON
    return None


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        text, antigravity = command_of(read_hook_stdin())
        reason = verdict(text)
        if reason:
            print(json.dumps(decision(reason, antigravity=antigravity)))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
