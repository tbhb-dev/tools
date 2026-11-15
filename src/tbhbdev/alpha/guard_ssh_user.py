# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-user`: name the account on ssh, scp and rsync.

A PreToolUse hook that shares guard-gh-pr's shell scanner and payload
contracts. It denies `ssh`, `scp` and `rsync` aimed at a configured host unless
the command names the `agent` or `ops` account. A bare host name falls back to
the local login, which is the operator's own account, and any other account
isn't one an agent session may use there.

The hosts come from `[harness] agent_user_hosts` in the user config. A name
matches a host of that name and its subdomains, ignoring case. A missing table
or key turns the guard off. Allowing prints nothing, and unreadable input, a
broken config or a crash fails open, like the other guards.

An account counts when it is the `user@` of the target, else the first `-l` or
`-o User=` of an ssh command, or of the `-e`/`--rsh` ssh command of rsync. Jump
hosts (`-J`, `-o ProxyJump=`) are targets too. Operands in `host:path`,
`scp://` and `rsync://` form are read, and `ssh://` for ssh.

An ssh config alias isn't resolved: the guard sees only the name typed, so an
alias counts only when it is listed. Known gaps: `sftp`, `mosh`, `git` over ssh,
`ssh -G` expansion of Host blocks, and a host held in a variable.
"""
# cspell:ignore proxyjump

import json
import re
import shlex
from typing import TYPE_CHECKING, NamedTuple

from tbhbdev import userconfig
from tbhbdev.alpha.guard_gh_pr import (
    command_of,
    decision,
    program_of,
    simple_commands,
)
from tbhbdev.alpha.repo import read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Iterator

TABLE = "harness"
KEY = "agent_user_hosts"
ALLOWED_USERS = {"agent", "ops"}
PROGRAMS = {"ssh", "scp", "rsync"}
# ssh options that take a value; the value is the rest of the word or the next.
SSH_VALUE_FLAGS = set("bcDEeFIiJLlmOoPpQRSWw")
RSH_FLAGS = {"-e", "--rsh"}
REASON = (
    "The command reaches {host} without an agent@ or ops@ account, so it would "
    "log in as the local user. Write agent@{host} or ops@{host}."
)
URL = re.compile(
    r"(?:scp|rsync|ssh)://(?:(?P<user>[^@/]+)@)?(?P<host>\[[^\]]+\]|[^:/@]+)(?::\d+)?(?:/|$)"
)
REMOTE = re.compile(r"(?:(?P<user>[^@/:\s]+)@)?(?P<host>\[[^\]]+\]|[^@/:\s\[]+):")
PORT = re.compile(r":\d+$")


class Target(NamedTuple):
    """A host a command reaches and the account it names, if any."""

    host: str
    user: str | None


def split_target(text: str) -> tuple[str | None, str]:
    """``(user, host)`` of an ssh destination, with any scheme and port removed."""
    found = URL.fullmatch(text) or URL.match(text)
    if found:
        return found["user"], found["host"].strip("[]")
    user, at, host = text.rpartition("@")
    return (user if at else None), PORT.sub("", host).strip("[]")


def option_user(option: str) -> str | None:
    """The account of an ``-o`` value like ``User=agent`` or ``User agent``."""
    key, _, value = option.replace("=", " ", 1).partition(" ")
    return value.strip() if key.lower() == "user" and value.strip() else None


def option_of(word: str, args: list[str], i: int) -> tuple[tuple[str, str] | None, int]:
    """The first value-taking option in a ``-xyz`` word, and the next index."""
    for offset, letter in enumerate(word[1:], start=2):
        if letter in SSH_VALUE_FLAGS:
            value = word[offset:]
            if not value and i < len(args):
                value = args[i]
                i += 1
            return (letter, value), i
    return None, i


def ssh_words(args: list[str]) -> Iterator[tuple[str, str]]:
    """Each ssh option as ``(letter, value)``, then the operands as ``("", word)``."""
    i = 0
    while i < len(args):
        word = args[i]
        i += 1
        if word == "--":
            for rest in args[i:]:
                yield "", rest
            return
        if not word.startswith("-") or word == "-":
            yield "", word
            continue
        found, i = option_of(word, args, i)
        if found:
            yield found


def ssh_login(args: list[str]) -> str | None:
    """The first account an ssh command line sets with ``-l`` or ``-o User=``."""
    for letter, value in ssh_words(args):
        if letter == "l":
            return value
        if letter == "o" and (user := option_user(value)):
            return user
    return None


def jump_targets(value: str) -> Iterator[Target]:
    """The targets of a ``-J`` or ``ProxyJump`` list."""
    for hop in value.split(","):
        user, host = split_target(hop)
        yield Target(host, user)


def ssh_targets(args: list[str]) -> Iterator[Target]:
    """The destination and jump hosts of an ssh command line."""
    login = ssh_login(args)
    destination = False
    for letter, value in ssh_words(args):
        if letter == "J":
            yield from jump_targets(value)
        elif letter == "o" and value.lower().replace("=", " ").startswith("proxyjump "):
            yield from jump_targets(
                value.replace("=", " ", 1).partition(" ")[2].strip()
            )
        elif letter == "" and not destination:
            destination = True
            user, host = split_target(value)
            yield Target(host, user or login)


def remote_shell(args: list[str]) -> list[str]:
    """The words of an rsync ``-e`` or ``--rsh`` command, or none."""
    for i, word in enumerate(args):
        value = None
        if word in RSH_FLAGS and i + 1 < len(args):
            value = args[i + 1]
        elif word.startswith("--rsh="):
            value = word[len("--rsh=") :]
        elif word.startswith("-e") and not word.startswith("--"):
            value = word[2:]
        if value is not None:
            try:
                return shlex.split(value)[1:]
            except ValueError:
                return []
    return []


def operand_target(word: str, fallback: str | None) -> Target | None:
    """The target of an scp or rsync operand, or None for a local path."""
    found = URL.match(word) or REMOTE.match(word)
    if not found:
        return None
    return Target(found["host"].strip("[]"), found["user"] or fallback)


def copy_targets(program: str, args: list[str]) -> Iterator[Target]:
    """Every remote operand of an scp or rsync command line."""
    shell = remote_shell(args) if program == "rsync" else args
    fallback = ssh_login(shell)
    for word in args:
        target = operand_target(word, fallback)
        if target:
            yield target
    if program == "scp":
        for letter, value in ssh_words(args):
            if letter == "J":
                yield from jump_targets(value)


def targets(words: list[str]) -> Iterator[Target]:
    """The hosts a simple command reaches, for ssh, scp and rsync."""
    program = program_of(words)
    if program == "ssh":
        yield from ssh_targets(words[1:])
    elif program in PROGRAMS:
        yield from copy_targets(program, words[1:])


def guarded(host: str, names: list[str]) -> bool:
    """Whether ``host`` is a listed name or a subdomain of one."""
    lowered = host.lower().rstrip(".")
    return any(lowered == name or lowered.startswith(f"{name}.") for name in names)


def verdict(text: str, names: list[str]) -> str | None:
    """The deny reason for a command, or None to allow it."""
    for _, words in simple_commands(text):
        for target in targets(words):
            if guarded(target.host, names) and target.user not in ALLOWED_USERS:
                return REASON.format(host=target.host)
    return None


def hosts() -> list[str]:
    """The guarded host names from the user config, lowercase, or none."""
    entries = userconfig.optional_strings(TABLE, KEY)
    return [entry.lower() for entry in entries or []]


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        text, antigravity = command_of(read_hook_stdin())
        reason = verdict(text, hosts())
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    if reason:
        print(json.dumps(decision(reason, antigravity=antigravity)))
    return 0
