# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Keep git hooks in force; ``guard-gh-poll`` runs this check.

It uses guard_gh_pr's shell-word scanner. The pre-push guard and the
other git hooks only work while git runs them, so this denies the ways a
command makes git skip or swap them:

    git -c core.hooksPath=DIR ...        also ``--config-env`` and an
                                         assignment such as
                                         ``GIT_CONFIG_PARAMETERS=...``
    git config core.hooksPath ...        any form that writes the key; reads
                                         such as ``--get`` pass
    git ... --no-verify                  on any git command

This is an accident guard: a script file, a variable that
holds the key and ``bash -c`` forms outside the scanner's list are out of
scope.
"""

from typing import TYPE_CHECKING

from tbhbdev.alpha.guard_gh_pr import (
    program_of,
    simple_commands,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

KEY = "core.hookspath"
READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--show-origin", "--list", "-l"}
REASON = (
    "Blocked: never bypass a git hook or guard. Do not use `-c core.hooksPath`, "
    "write core.hooksPath or pass `--no-verify`. If a push is refused, push the "
    "work to a new branch and open a pull request."
)


def names_key(words: Iterable[str]) -> bool:
    """Whether any word mentions the hooks path key, in any letter case."""
    return any(KEY in word.lower() for word in words)


def bypasses(prefix: list[str], words: list[str]) -> bool:
    """Whether a simple command skips or redirects git hooks."""
    if names_key(prefix):
        return True
    if program_of(words) != "git":
        return False
    if "--no-verify" in words:
        return True
    if not names_key(words):
        return False
    if "config" in words:
        return not READ_FLAGS.intersection(words)
    return True


def verdict(text: str) -> str | None:
    """The deny reason for a command, or None to allow it."""
    for prefix, words in simple_commands(text):
        if bypasses(prefix, words):
            return REASON
    return None
