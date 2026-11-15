# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Keep git hooks in force; ``guard-gh-poll`` runs this check.

It uses guard_gh_pr's shell-word scanner. The pre-push guard and the
other git hooks only work while git runs them, so this denies the ways a
command makes git skip or swap them:

    git -c core.hooksPath=DIR ...        also ``--config-env`` and an
                                         assignment such as
                                         ``GIT_CONFIG_PARAMETERS=...``
    git config core.hooksPath VALUE      a write; reads (``--get``, or the key
                                         alone) pass
    export GIT_CONFIG_PARAMETERS=...     ``export`` and ``declare`` naming the key
    git ... --no-verify                  also ``--no-ve`` and ``commit -n``

This is an accident guard: a script file, a variable that
holds the key and ``bash -c`` forms outside the scanner's list are out of
scope.
"""

import re
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard_gh_pr import (
    program_of,
    simple_commands,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

KEY = "core.hookspath"
READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--show-origin", "--list", "-l"}
SETTERS = {"export", "declare", "typeset", "readonly", "local"}
NO_VERIFY = "--no-verify"
FLAG_LETTERS = ["a", "i", "s", "v", "q", "e", "z", "o", "p"]
SHORT_N = re.compile(
    "-[" + "".join(FLAG_LETTERS) + "]*n[" + "".join(FLAG_LETTERS) + "]*"
)
# Options whose next word is a value, never a flag.
VALUE_OPTIONS = {"-m", "--message", "--grep", "-F", "-C", "-c", "-e", "-S", "-G"}
FILE_OPTIONS = {"-f", "--file", "--blob", "--default", "--type"}
READ_WORDS = {"get", "list"}
ENV_PREFIX = "git_config_"
REASON = (
    "Blocked: never bypass a git hook or guard. Do not use `-c core.hooksPath`, "
    "write core.hooksPath or pass `--no-verify`. If a push is refused, push the "
    "work to a new branch and open a pull request."
)


def names_key(words: Iterable[str]) -> bool:
    """Whether any word mentions the hooks path key, in any letter case."""
    return any(KEY in word.lower() for word in words)


def sets_key_inline(words: list[str]) -> bool:
    """Whether git's own options set the key: ``-c``, ``-cKEY=V``, ``--config-env``."""
    for at, word in enumerate(words):
        lower = word.lower()
        if (
            lower in ("-c", "--config-env")
            and at + 1 < len(words)
            and KEY in words[at + 1].lower()
        ):
            return True
        if lower.startswith(("-c" + KEY, "--config-env=" + KEY)):
            return True
    return False


def writes_key(words: list[str]) -> bool:
    """Whether ``git config`` names the key with more than the key alone."""
    rest = words[words.index("config") + 1 :]
    if READ_FLAGS.union(READ_WORDS).intersection(rest):
        return False
    values = [
        word
        for at, word in enumerate(rest)
        if not word.startswith("-") and (at == 0 or rest[at - 1] not in FILE_OPTIONS)
    ]
    return len(values) > 1


def skips_verification(words: list[str]) -> bool:
    """Whether a git command passes --no-verify, an abbreviation or commit -n."""
    flags = [
        word
        for at, word in enumerate(words)
        if at == 0 or words[at - 1] not in VALUE_OPTIONS
    ]
    if any(w.startswith("--no-ve") and NO_VERIFY.startswith(w) for w in flags):
        return True
    return "commit" in words and any(SHORT_N.fullmatch(word) for word in flags)


def bypasses(words: list[str]) -> bool:
    """Whether a simple command skips or redirects git hooks."""
    if program_of(words) in SETTERS and names_key(words):
        return True
    if program_of(words) != "git":
        return False
    if sets_key_inline(words) or skips_verification(words):
        return True
    return "config" in words and names_key(words) and writes_key(words)


def verdict(text: str) -> str | None:
    """The deny reason for a command, or None to allow it."""
    lower = text.lower()
    if ENV_PREFIX in lower and KEY in lower:
        return REASON
    for _prefix, words in simple_commands(text):
        if bypasses(words):
            return REASON
    return None
