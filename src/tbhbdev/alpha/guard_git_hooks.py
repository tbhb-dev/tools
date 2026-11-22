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

It also gates ``git push`` rewrites. ``--force-with-lease`` (with or without
``=REF:SHA``) to a branch that isn't main or master passes. A plain
``--force``, ``-f`` (alone or inside a cluster such as ``-fu``), ``--mirror``,
a ``+REF`` refspec, ``--force-if-includes`` without a lease and any push to
main or master are denied. The pre-push hook cannot see which flag was used,
so this is where the lease semantic is enforced.

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
    "-[" + "".join(FLAG_LETTERS) + "]*n[" + "".join(FLAG_LETTERS) + "]*m?"
)
# Options whose next word is a value, never a flag.
VALUE_OPTIONS = {"-m", "--message", "--author", "--date", "-F", "-C", "-t"}
FILE_OPTIONS = {"-f", "--file", "--blob", "--default", "--type", "-t"}
READ_WORDS = {"get", "list", "unset"}
# A word that assigns a GIT_CONFIG_* variable, anywhere in the command text.
ENV_PREFIX = "git_config_"
ENV_ASSIGN = re.compile(r"(?:^|[\s;&|(])git_config_\w*=")
# A quoted span, blanked before ENV_ASSIGN looks: prose in a commit message isn't one.
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
# Git options whose next word is a value, so it is not the subcommand.
GLOBAL_VALUE_OPTIONS = {
    "-c",
    "-C",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
}
# Subcommands that run hooks, so ``--no-verify`` means something to them.
HOOK_COMMANDS = {
    "commit",
    "push",
    "merge",
    "am",
    "cherry-pick",
    "rebase",
    "revert",
    "pull",
}
REASON = (
    "Blocked: never bypass a git hook or guard. Do not use `-c core.hooksPath`, "
    "write core.hooksPath or pass `--no-verify`. If a push is refused, push the "
    "work to a new branch and open a pull request."
)


PUSH_REASON = (
    "Blocked: never force-push. Only `--force-with-lease` to a branch other "
    "than main or master is allowed; no `--force`, `-f`, `--mirror`, `+REF` "
    "refspec or push to main or master. Push a new branch and open a pull request."
)
PROTECTED_BRANCHES = {"main", "master"}
# Push options whose next word is a value, so it is not a refspec.
PUSH_VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
LEASE = "--force-with-lease"
INCLUDES = "--force-if-includes"
# Shortest abbreviation of --force or --mirror worth refusing: ``--mi`` is only
# --mirror, and ``--fo`` fails in git as ambiguous, so refusing it costs nothing.
MIN_ABBREVIATION = 4


def long_push_flag(word: str) -> str:
    """Classify a ``--`` push option as ``lease``, ``includes``, ``force`` or ``""``."""
    name = word.partition("=")[0]
    if name == LEASE:
        return "lease"
    if name == INCLUDES:
        return "includes"
    # Any other --force-* word is an abbreviation or unknown: fail closed.
    for full in ("--force", "--mirror"):
        if len(name) >= MIN_ABBREVIATION and full.startswith(name):
            return "force"
    return "force" if name.startswith("--force-") else ""


def short_push_flag(word: str) -> str:
    """Classify a ``-x`` push option cluster as ``force`` or ``""``."""
    for letter in word[1:]:
        if letter == "f":
            return "force"
        if letter == "o":
            break
    return ""


def push_flag(word: str) -> str:
    """Classify a push option as ``lease``, ``includes``, ``force`` or ``""``."""
    if word.startswith("--"):
        return long_push_flag(word)
    return short_push_flag(word)


def split_push_args(rest: list[str]) -> tuple[set[str], list[str]]:
    """The classified push options and the positional targets after them."""
    flags: set[str] = set()
    targets: list[str] = []
    options = True
    skip = False
    for word in rest:
        if skip:
            skip = False
        elif options and word == "--":
            options = False
        elif options and word.startswith("-"):
            flags.add(push_flag(word))
            skip = word in PUSH_VALUE_OPTIONS
        else:
            targets.append(word)
    return flags, targets


def targets_protected(targets: list[str]) -> bool:
    """Whether a push target is a ``+REF`` refspec or names a protected branch."""
    if any(target.startswith("+") for target in targets):
        return True
    for target in targets[1:]:
        destination = target.rpartition(":")[2].removeprefix("refs/heads/")
        if destination in PROTECTED_BRANCHES:
            return True
    return False


def push_reason(name: str, rest: list[str]) -> str | None:
    """The deny reason for a push that rewrites or targets protected history."""
    if name != "push":
        return None
    flags, targets = split_push_args(rest)
    if "force" in flags or ("includes" in flags and "lease" not in flags):
        return PUSH_REASON
    if targets_protected(targets):
        return PUSH_REASON
    return None


def names_key(words: Iterable[str]) -> bool:
    """Whether any word mentions the hooks path key, in any letter case."""
    return any(KEY in word.lower() for word in words)


def names_key_setting(value: str) -> bool:
    """Whether ``KEY`` or ``KEY=VALUE`` names the hooks path key itself."""
    return value.lower().partition("=")[0].strip() == KEY


def assigns_key(lower: str) -> bool:
    """Whether the text assigns a ``GIT_CONFIG_*`` variable a value naming the key.

    The assignment itself must sit outside quotes, as prose in a message doesn't
    set anything. The value runs to the next unquoted space.
    """
    blank = QUOTED.sub(lambda found: " " * len(found.group()), lower)
    for found in ENV_ASSIGN.finditer(blank):
        end = found.end()
        while end < len(lower) and (
            not blank[end].isspace() or lower[end] != blank[end]
        ):
            end += 1
        if KEY in lower[found.end() : end]:
            return True
    return False


def assigns_env(prefix: list[str]) -> bool:
    """Whether a command's prefix assigns ``GIT_CONFIG_*`` and names the key."""
    return any(
        w.lower().startswith(ENV_PREFIX) and "=" in w and KEY in w.lower()
        for w in prefix
    )


def subcommand(words: list[str]) -> tuple[int, str]:
    """The index and name of the git subcommand, after git's own options."""
    at = 1
    while at < len(words):
        word = words[at]
        if word in GLOBAL_VALUE_OPTIONS:
            at += 2
        elif word.startswith("-"):
            at += 1
        else:
            return at, word
    return len(words), ""


def sets_key_inline(words: list[str]) -> bool:
    """Whether git's own options set the key: ``-c``, ``-cKEY=V``, ``--config-env``."""
    for at, word in enumerate(words):
        lower = word.lower()
        if lower in ("-c", "--config-env"):
            if at + 1 < len(words) and names_key_setting(words[at + 1]):
                return True
        elif lower.startswith("-c"):
            if names_key_setting(word[2:]):
                return True
        elif lower.startswith("--config-env=") and names_key_setting(word[13:]):
            return True
    return False


def writes_key(rest: list[str]) -> bool:
    """Whether ``git config`` names the key with more than the key alone."""
    if READ_FLAGS.intersection(rest):
        return False
    values = [
        word
        for at, word in enumerate(rest)
        if not word.startswith("-") and (at == 0 or rest[at - 1] not in FILE_OPTIONS)
    ]
    return len(values) > 1 and values[0] not in READ_WORDS


def skips_verification(name: str, rest: list[str]) -> bool:
    """Whether a hook-running git command passes --no-verify, an abbreviation or -n."""
    if name not in HOOK_COMMANDS:
        return False
    flags = [
        word
        for at, word in enumerate(rest)
        if at == 0 or rest[at - 1] not in VALUE_OPTIONS
    ]
    if any(w.startswith("--no-ve") and NO_VERIFY.startswith(w) for w in flags):
        return True
    return name == "commit" and any(SHORT_N.fullmatch(word) for word in flags)


def bypasses(words: list[str]) -> bool:
    """Whether a simple command skips or redirects git hooks."""
    if program_of(words) in SETTERS and names_key(words):
        return True
    if program_of(words) != "git":
        return False
    at, name = subcommand(words)
    if sets_key_inline(words[:at]):
        return True
    rest = words[at + 1 :]
    if skips_verification(name, rest):
        return True
    return name == "config" and names_key(rest) and writes_key(rest)


def verdict(text: str) -> str | None:
    """The deny reason for a command, or None to allow it."""
    lower = text.lower()
    if assigns_key(lower):
        return REASON
    for prefix, words in simple_commands(text):
        if assigns_env(prefix) or bypasses(words):
            return REASON
        if program_of(words) == "git":
            at, name = subcommand(words)
            if why := push_reason(name, words[at + 1 :]):
                return why
    return None
