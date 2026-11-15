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

An account counts when it is the first `-l` or `-o User=` of an ssh command, or
of the `-e`/`--rsh` ssh command of rsync (the last one given), as `ssh -G` reads
it. Without one it is the `user@` of the target. scp's own `-l` limits
bandwidth, so only its `-o User=` counts. Jump hosts (`-J`, `-o ProxyJump=`),
the `-o HostName=` host and the words of an `-o ProxyCommand=` are targets too.
Operands in `host:path`, `scp://` and `rsync://` form are read, and `ssh://` for
ssh. Wrappers such as `timeout`, `nice`, `sudo`, `env` and `xargs` are skipped
with their options, redirections are dropped, and a redirection cut off a word.

An ssh config alias isn't resolved: the guard sees only the name typed, so an
alias counts only when it is listed. Known gaps: `sftp`, `mosh`, `git` over ssh,
`ssh -G` expansion of Host blocks, a host held in a variable or fed on stdin
(`xargs ssh`), and a clustered rsync `-ae` shell option.
"""
# cspell:ignore proxyjump proxycommand caffeinate sshpass

import json
import re
import shlex
from typing import TYPE_CHECKING, NamedTuple

from tbhbdev import userconfig
from tbhbdev.alpha.guard_gh_pr import (
    ASSIGNMENT,
    KEYWORDS,
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
# Programs that run another one: option flags that take a value, and the
# operands that come before the program. guard-gh-pr's list is its own.
WRAPPERS = {
    "env": ({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}, 0),
    "sudo": ({"-u", "-g", "-C", "-D", "-h", "-p", "-R", "-T", "-U", "--user"}, 0),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1),
    "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "-n", "-p"}, 0),
    "stdbuf": ({"-i", "-o", "-e"}, 0),
    "caffeinate": ({"-t", "-w"}, 0),
    "sshpass": ({"-p", "-f", "-d", "-P"}, 0),
    "xargs": ({"-n", "-L", "-P", "-I", "-s", "-E", "-d", "-a", "-J", "-R"}, 0),
    "setsid": (set(), 0),
    "nohup": (set(), 0),
    "time": (set(), 0),
    "command": (set(), 0),
    "exec": (set(), 0),
    "builtin": (set(), 0),
}
REDIRECT = re.compile(r"\d*(?:>>|<<|<>|[<>])")
# The & of 2>&1 and &> belongs to the redirection; the scanner would split there.
AMPERSAND = re.compile(r"(?<=[<>])&|&(?=>)")
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


def option_pair(option: str) -> tuple[str, str]:
    """The lowercase key and the value of an ``-o`` word like ``User=agent``."""
    key, _, value = option.replace("=", " ", 1).partition(" ")
    return key.lower(), value.strip()


def option_user(option: str) -> str | None:
    """The account of an ``-o`` value like ``User=agent`` or ``User agent``."""
    key, value = option_pair(option)
    return value if key == "user" and value else None


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


def ssh_login(args: list[str], letters: str = "lo") -> str | None:
    """The first account set with ``-l`` or ``-o User=``, as ``ssh -G`` reads it.

    ``letters`` names the options that count: scp's ``-l`` limits bandwidth.
    """
    for letter, value in ssh_words(args):
        if letter == "l" and "l" in letters:
            return value
        if letter == "o" and (user := option_user(value)):
            return user
    return None


def shell_words(text: str) -> list[str]:
    """``text`` split like a shell, or no words when a quote never closes."""
    try:
        return shlex.split(text)
    except ValueError:
        return []


def jump_targets(value: str) -> Iterator[Target]:
    """The targets of a ``-J`` or ``ProxyJump`` list."""
    for hop in value.split(","):
        user, host = split_target(hop)
        yield Target(host, user)


def option_targets(value: str, user: str | None) -> Iterator[Target]:
    """The hosts an ``-o`` value names: ``ProxyJump``, ``HostName``, ``ProxyCommand``.

    ``user`` is the account the connection itself uses, which a ``HostName``
    host receives. Each word of a ``ProxyCommand`` counts as a target.
    """
    key, rest = option_pair(value)
    if key == "proxyjump":
        yield from jump_targets(rest)
    elif key == "hostname":
        yield Target(rest, user)
    elif key == "proxycommand":
        for word in shell_words(rest):
            account, host = split_target(word)
            yield Target(host, account)


def option_hosts(args: list[str], user: str | None) -> Iterator[Target]:
    """The jump and option-named hosts of an ssh command line."""
    for letter, value in ssh_words(args):
        if letter == "J":
            yield from jump_targets(value)
        elif letter == "o":
            yield from option_targets(value, user)


def ssh_targets(args: list[str]) -> Iterator[Target]:
    """The destination and the other hosts of an ssh command line."""
    destination = next((v for letter, v in ssh_words(args) if letter == ""), None)
    user, host = split_target(destination) if destination else (None, "")
    user = ssh_login(args) or user
    yield from option_hosts(args, user)
    if destination:
        yield Target(host, user)


def remote_shell(args: list[str]) -> list[str]:
    """The words of the last rsync ``-e`` or ``--rsh`` command, or none."""
    shell: list[str] = []
    for i, word in enumerate(args):
        value = None
        if word in RSH_FLAGS and i + 1 < len(args):
            value = args[i + 1]
        elif word.startswith("--rsh="):
            value = word[len("--rsh=") :]
        elif word.startswith("-e") and not word.startswith("--"):
            value = word[2:]
        if value is not None:
            shell = shell_words(value)[1:]
    return shell


def operand_target(word: str, login: str | None) -> Target | None:
    """The target of an scp or rsync operand, or None for a local path.

    A ``login`` from the ssh options wins over the operand's ``user@``.
    """
    found = URL.match(word) or REMOTE.match(word)
    if not found:
        return None
    return Target(found["host"].strip("[]"), login or found["user"])


def copy_targets(program: str, args: list[str]) -> Iterator[Target]:
    """Every remote operand and jump host of an scp or rsync command line."""
    shell = remote_shell(args) if program == "rsync" else args
    login = ssh_login(shell, "lo" if program == "rsync" else "o")
    for word in args:
        target = operand_target(word, login)
        if target:
            yield target
    yield from option_hosts(shell, login)


def strip_redirects(words: list[str]) -> list[str]:
    """``words`` without shell redirections, and with any redirection cut off a word."""
    kept: list[str] = []
    target_next = False
    for word in words:
        found = REDIRECT.match(word)
        if target_next:
            target_next = False
        elif found:
            target_next = found.end() == len(word)
        else:
            kept.append(re.split("[<>]", word, maxsplit=1)[0])
    return kept


def after_wrapper(words: list[str], i: int, name: str) -> int:
    """The index after a wrapper's options and its fixed operands."""
    value_flags, operands = WRAPPERS[name]
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        if words[i] == "--":
            i += 1
            break
        i += 2 if words[i] in value_flags else 1
    return min(i + operands, len(words))


def program_words(words: list[str]) -> list[str]:
    """The words from the program, after assignments, keywords and wrappers."""
    i = 0
    while i < len(words):
        name = program_of(words[i:])
        if words[i] in KEYWORDS or ASSIGNMENT.match(words[i]):
            i += 1
        elif name in WRAPPERS:
            i = after_wrapper(words, i + 1, name)
        else:
            break
    return words[i:]


def targets(command: list[str]) -> Iterator[Target]:
    """The hosts a simple command reaches, for ssh, scp and rsync."""
    words = program_words(strip_redirects(command))
    program = program_of(words) if words else ""
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
    for simple in simple_commands(AMPERSAND.sub("", text)):
        for target in targets([*simple.prefix, *simple.words]):
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
