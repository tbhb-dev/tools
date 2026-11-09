# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-gh-poll`: block GitHub polling and token unsets.

A PreToolUse hook beside ``guard-gh-pr``, with the same payload handling and
output contracts (``guard_gh_pr.command_of`` and ``decision``) and the same
shell-word scanner. GitHub CI and pull request state comes from awrel, which
relays webhook events into a session, so an agent never needs to poll GitHub.
These rules apply in every repository, and deny:

    gh run watch, gh run list, gh pr checks --watch
    gh api|query --paginate                      any endpoint
    gh api|query repos/O/R/actions/runs          and the other list endpoints:
                                                 actions/workflows/W/runs,
                                                 actions/jobs, check-runs,
                                                 check-suites
    while|until|for loops whose condition or body runs gh, or runs both
                                                 sleep and pgrep

The loop rule covers the self-matching wait, ``while pgrep -f fetch.py; do
sleep 5; done``: pgrep -f matches its own shell's command line, so the loop
never ends. Single-object reads pass: ``actions/runs/ID``,
``actions/runs/ID/jobs``, ``actions/jobs/ID``, ``check-runs/ID``,
``actions/runs?head_sha=SHA`` for one full 40-hex SHA, ``pulls/N``,
``commits/SHA/check-runs`` and ``rate_limit``. A list endpoint with an
explicit non-GET method (``-X POST``), or with fields and no method, which gh
sends as a POST, is a write, not a read, and passes too. The deny reason tells the
agent to subscribe to the awrel event instead, and names the way to get an
exception.

It also denies, so a command keeps the environment the harness gave it, what
removes or empties the 1Password service account token's variable (``TOKEN``):

    unset TOKEN                                  also ``unset ${TOKEN}`` and
                                                 ``unset "$TOKEN"``, which
                                                 name it literally
    env -u TOKEN, -uTOKEN, --unset[=| ]TOKEN     in option clusters (``-iuTOKEN``)
                                                 and after other options that
                                                 take a value (``-C DIR``,
                                                 ``--chdir=DIR``), and inside
                                                 ``env -S '...'``
    env -i, env --ignore-environment, env -      they drop it, whatever the
                                                 rest of the command is
    export -n TOKEN, declare|typeset|local +x TOKEN
    TOKEN= cmd, TOKEN='' cmd, TOKEN="", export TOKEN=
                                                 an assignment that empties it

A variable that holds the name (``n=TOKEN; unset "$n"``) is out of scope: the
scanner doesn't expand variables.

``bash|sh|zsh|dash|ksh -c SCRIPT`` (``-lc`` too), ``eval ...`` and ``ssh
[options] HOST COMMAND...`` are scanned recursively, and so is a heredoc body
fed to a shell or ssh that has no script of its own (``bash <<EOF``). Any
other heredoc body, and a quoted argument to any other command, is data:
``git commit -m 'stop gh run list'`` and an ``echo`` of an unset command
pass. A loop that runs ``bash -c 'gh ...'``
counts as running gh.

Allowing prints nothing. Like ``guard-gh-pr``, unreadable input or any crash
allows the command and prints nothing, because a broken hook must never stop
the agent's work. This is an accident guard, not a security boundary. It
misses a wrapper outside guard_gh_pr's list (``timeout 5 gh``, ``watch gh``),
a script file, alias or function that runs gh, code run from a quoted argument
to anything other than a shell, eval or ssh (``python -c``), a shell fed by a
pipe (``echo 'gh run list' | sh``), substitutions inside an unquoted heredoc
body, other API clients such as curl, and a variable that holds the token's
name. ``tbhbdev alpha
gh-shim`` budgets the gh calls a script makes, which no hook can see.
"""

import itertools
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import override
from urllib.parse import parse_qs, urlsplit

from tbhbdev.alpha.guard_gh_pr import (
    API_FIELD_FLAGS,
    API_VALUE_FLAGS,
    ASSIGNMENT,
    KEYWORDS,
    SHELLS,
    VALUE_FLAGS,
    WRAPPERS,
    Scanner,
    command_of,
    decision,
    flag_values,
    program_of,
    shell_script,
    split_args,
)
from tbhbdev.alpha.repo import read_hook_stdin

POLL_REASON = (
    "Don't poll GitHub. Subscribe to the awrel event instead, for example "
    "check_run conclusion events for the repository through awrel, and act when "
    "it arrives. Bulk and polling GitHub reads are blocked; ask the operator "
    "for a capped exception."
)
# The token's variable, split because the repository's gitleaks config flags
# the name written out in full.
TOKEN = "OP_SERVICE_" + "ACCOUNT_TOKEN"
UNSET_REASON = f"Don't unset {TOKEN}. Leave the environment as the harness gives it."
# The list endpoints, after any scheme, host and API prefix.
LIST_PATH = re.compile(
    r"(?:.*/)?repos/[^/]+/[^/]+/(?:actions/runs|actions/workflows/[^/]+/runs"
    r"|actions/jobs|check-runs|check-suites)"
)
RUNS_PATH = re.compile(r"(?:.*/)?repos/[^/]+/[^/]+/actions/runs")
SHA = re.compile(r"[0-9a-fA-F]{40}")
LOOPS = {"while", "until", "for"}
# env's long options, each with the short option it spells ("" for the rest).
ENV_LONG = {
    "ignore-environment": "i", "null": "0", "unset": "u", "chdir": "C",
    "split-string": "S", "debug": "v", "argv0": "a", "block-signal": "",
    "default-signal": "", "ignore-signal": "", "list-signal-handling": "",
    "help": "", "version": "",
}  # fmt: skip
# env's short options that take a value, attached or as the next word.
ENV_VALUE = {"u", "C", "S", "a", "P"}
# The builtins that can drop a variable's export, and the (sign, letter) that does.
UNEXPORT = {
    "export": ("-", "n"),
    "declare": ("+", "x"),
    "typeset": ("+", "x"),
    "local": ("+", "x"),
}
# ssh's options that take a value; the next word is not the host.
SSH_VALUE_FLAGS = {
    "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}  # fmt: skip


@dataclass
class BodyScanner(Scanner):
    """guard_gh_pr's scanner, keeping the heredoc text read after each command.

    ``bodies`` maps a command's index in the scan to the heredoc text skipped
    at the end of its line, delimiter lines included.
    """

    bodies: dict[int, str] = field(default_factory=dict)
    pending: list[int] = field(default_factory=list)

    @override
    def heredoc(self) -> None:
        """Note the command a heredoc belongs to, then read its delimiter."""
        self.pending.append(len(self.commands))
        super().heredoc()

    @override
    def skip_heredoc_bodies(self) -> None:
        """Skip the queued bodies, keeping their text for their commands."""
        start = self.pos
        super().skip_heredoc_bodies()
        for index in self.pending:
            self.bodies[index] = self.text[start : self.pos]
        self.pending = []


def bulk_api(args: list[str]) -> bool:
    """Whether ``gh api`` arguments page, or read a list endpoint."""
    flags, endpoints = split_args(args, API_VALUE_FLAGS)
    if any(flag.partition("=")[0] == "--paginate" for flag in flags):
        return True
    methods = flag_values(args, {"-X", "--method"}, API_VALUE_FLAGS)
    if methods:
        if methods[-1].upper() not in {"GET", "HEAD"}:
            return False
    elif flag_values(args, API_FIELD_FLAGS, API_VALUE_FLAGS) or "--input" in flags:
        return False
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        path = parsed.path.rstrip("/")
        if not LIST_PATH.fullmatch(path):
            continue
        heads = parse_qs(parsed.query).get("head_sha", [])
        one_sha = len(heads) == 1 and SHA.fullmatch(heads[0])
        if not (RUNS_PATH.fullmatch(path) and one_sha):
            return True
    return False


def github_poll(args: list[str]) -> bool:
    """Whether the words after ``gh``, past its global flags, poll GitHub."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in VALUE_FLAGS else 1
    rest = args[i:]
    if rest[:2] in (["run", "watch"], ["run", "list"]):
        return True
    if rest[:2] == ["pr", "checks"]:
        return any(w.partition("=")[0] == "--watch" for w in rest[2:])
    return rest[:1] in (["api"], ["query"]) and bulk_api(rest[1:])


def names_token(word: str) -> bool:
    """Whether a word is the token's name: ``TOKEN``, ``$TOKEN`` or ``${TOKEN}``."""
    return word in {TOKEN, f"${TOKEN}", f"${{{TOKEN}}}"}


def split_string(text: str) -> list[str]:
    """The words of env's ``-S`` string, split as the shell would."""
    try:
        return shlex.split(text)
    except ValueError:  # an unbalanced quote
        return text.split()


def next_word(args: list[str]) -> str:
    """Take the next word of ``args`` as an option's value; "" when none is left."""
    return args.pop(0) if args else ""


def env_option(word: str, args: list[str]) -> tuple[str, str]:
    """The (letter, value) of one env option word; a separate value leaves ``args``.

    The letter is the short option's, for a long option too (``--unset`` is
    ``u``), or "" for an option that takes no value and isn't ``-i``. A long
    option may be any unique prefix. A cluster such as ``-iuNAME`` reports
    its first ``i`` or value option: ``i`` is denied whatever follows, and a
    value option takes the rest of the cluster.
    """
    if word == "-":
        return "i", ""
    if word.startswith("--"):
        name, equals, value = word[2:].partition("=")
        names = [long for long in ENV_LONG if long.startswith(name)]
        letter = ENV_LONG[names[0]] if len(names) == 1 else ""
        if letter in ENV_VALUE and not equals:
            value = next_word(args)
        return letter, value
    return cluster_option(word[1:], args)


def cluster_option(cluster: str, args: list[str]) -> tuple[str, str]:
    """The first ``i`` or value option of a cluster of short options, with its value."""
    for i, char in enumerate(cluster, 1):
        if char == "i":
            return "i", ""
        if char in ENV_VALUE:
            return char, cluster[i:] or next_word(args)
    return "", ""


def env_rest(options: list[str]) -> list[str] | None:
    """The words after env's options; None when env drops or unsets the token."""
    args = list(options)
    while args and args[0].startswith("-"):
        word = args.pop(0)
        if word == "--":
            break
        letter, value = env_option(word, args)
        if letter == "i" or (letter == "u" and names_token(value)):
            return None
        if letter == "S":
            args = split_string(value) + args
    return args


def clears_token(program: str, args: list[str]) -> bool:
    """Whether ``export -n``, ``declare +x`` and the like drop or empty the token."""
    if program not in UNEXPORT:
        return False
    sign, letter = UNEXPORT[program]
    options = list(itertools.takewhile(lambda w: w.startswith(("-", "+")), args))
    drops = any(o[0] == sign and letter in o[1:] for o in options)
    return any(
        word == f"{TOKEN}=" or (drops and names_token(word.partition("=")[0]))
        for word in args[len(options) :]
    )


def unwrapped(words: list[str]) -> list[str] | None:
    """The program's words after prefixes; None when they unset the token.

    Prefixes are guard_gh_pr's: ``NAME=value`` words, shell keywords and its
    wrappers with their options. env's options are parsed as env does.
    ``TOKEN=`` empties the token.
    """
    rest: list[str] | None = words
    while rest:
        word = rest[0]
        wrapper = program_of(rest)
        if word == f"{TOKEN}=":
            return None
        if word in KEYWORDS or ASSIGNMENT.match(word):
            rest = rest[1:]
        elif wrapper == "env":
            rest = env_rest(rest[1:])
        elif wrapper in WRAPPERS:
            rest = rest[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
        else:
            break
    return rest


def remote_command(args: list[str]) -> str | None:
    """The remote command of ssh after its options and host; None without one."""
    rest = iter(args)
    for word in rest:
        if word in SSH_VALUE_FLAGS:
            next(rest, None)
        elif not word.startswith("-"):
            return " ".join(rest) or None
    return None


def opens_loop(command: list[str]) -> bool:
    """Whether a simple command starts a while, until or for loop."""
    for word in command:
        if word in LOOPS:
            return True
        if word not in KEYWORDS:
            return False
    return False


def inner_script(words: list[str], body: str | None) -> str | None:
    """The script a shell, eval or ssh runs, or None for any other program."""
    program, args = program_of(words), words[1:]
    if program in SHELLS:
        script = shell_script(args)
        reads_stdin = all(word.startswith("-") for word in args)
        return body if script is None and reads_stdin else script
    if program == "eval":
        return " ".join(args)
    if program == "ssh":
        script = remote_command(args)
        return body if script is None else script
    return None


def denial(command: list[str], body: str | None) -> tuple[str | None, set[str]]:
    """One simple command's deny reason, and the programs it runs."""
    words = unwrapped(command)
    if words is None:
        return UNSET_REASON, set()
    if not words:
        return None, set()
    program = program_of(words)
    unset = program == "unset" and any(names_token(word) for word in words[1:])
    if unset or clears_token(program, words[1:]):
        return UNSET_REASON, set()
    if program == "gh" and github_poll(words[1:]):
        return POLL_REASON, set()
    script = inner_script(words, body)
    if script is None:
        return None, {program}
    message, inner = inspect(script)
    return message, {program, *inner}


def inspect(text: str) -> tuple[str | None, set[str]]:
    """The deny reason for a shell command, or None, and the programs it runs.

    Each open loop collects the programs run from its keyword to its
    ``done``; an outer loop also collects its inner loops' programs.
    """
    loops: list[set[str]] = []
    programs: set[str] = set()
    scanner = BodyScanner(text)
    for index, command in enumerate(scanner.run()):
        if opens_loop(command):
            loops.append(set())
        message, seen = denial(command, scanner.bodies.get(index))
        if message:
            return message, programs
        programs |= seen
        for loop in loops:
            loop.update(seen)
            if "gh" in loop or {"sleep", "pgrep"} <= loop:
                return POLL_REASON, programs
        if seen == {"done"} and loops:
            loops.pop()
    return None, programs


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        text, antigravity = command_of(read_hook_stdin())
        message = inspect(text)[0]
        if message:
            print(json.dumps(decision(message, antigravity=antigravity)))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
