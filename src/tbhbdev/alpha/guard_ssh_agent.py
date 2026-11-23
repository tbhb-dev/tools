# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-agent`: keep agent commands off the SSH agent.

This PreToolUse hook shares guard-gh-pr's payload contracts but parses the
command with its own narrow shlex pass. It splits the command at shell
operators, strips `env`, `sudo`, `command`, and similar wrappers, and denies:

- ssh, scp, sftp, and rsync over ssh, unless the options before the host
  include `-o IdentityAgent=none` as the first IdentityAgent option and an
  explicit `-i KEY`;
- `ssh-add`;
- a path argument that names the operator's ssh directory in their home,
  except its `sessions` subdirectory.

Text that only mentions a variable or a path, such as a commit message, is
never matched. Substitutions in `$(...)` and backticks are parsed again. Like
the other guards, allowing prints nothing and unreadable input or a crash fails
open.

Known gaps: a program name held in a variable, runners such as `timeout`,
`GIT_SSH_COMMAND` and `core.sshCommand` settings, ssh config files named with
`-F`, a heredoc body that starts a line with a program name, and a git
transport that starts ssh below the shell command.
"""

import json
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard_gh_pr import command_of, decision
from tbhbdev.alpha.repo import read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Mapping

REASON_AGENT = (
    "The ssh call is blocked: pass -o IdentityAgent=none and an explicit "
    "-i KEY before the host, so it never reaches the SSH agent."
)
REASON_ADD = "ssh-add is blocked: agent sessions never load keys into an agent."
REASON_DIR = (
    "The command is blocked: it names a path in the operator's ssh directory "
    "outside its sessions subdirectory."
)
SSH_DIR = ".ssh"
OPERATORS = ";&|()<>\n"
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
AGENT_OPTION = re.compile(r"\s*IdentityAgent\s*[=\s]\s*(\S+)", re.IGNORECASE)
SUBSTITUTION = re.compile(r"\$\((.*)\)|`([^`]*)`", re.DOTALL)
# Letters of each program's options that take a value.
TAKES_VALUE = {
    "ssh": "BbcDEeFIiJLlmOopQRSWw",
    "scp": "cDFiJloPSX",
    "sftp": "BbcDFiJloPRSsX",
}
# Wrapper programs and the options of each that take a value.
WRAPPERS = {
    "env": {"-u", "-C", "-S"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
    "command": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": set(),
    "nice": {"-n"},
}
SHELLS = {"bash", "sh", "zsh", "dash"}
PRINTERS = {"echo", "printf"}


def commands(text: str) -> list[list[str]]:
    """The simple commands of TEXT as word lists, split at shell operators.

    Raises:
        ValueError: TEXT has an unbalanced quote.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=OPERATORS)
    lexer.whitespace = " \t\r"  # noqa: V101  # shlex reads this
    lexer.whitespace_split = True  # noqa: V101  # shlex reads this
    found: list[list[str]] = [[]]
    for token in lexer:
        if all(char in OPERATORS for char in token):
            found.append([])
        else:
            found[-1].append(token)
    return [words for words in found if words]


def skip_options(words: list[str], index: int, takes: set[str]) -> int:
    """The index after the options at INDEX, where TAKES lists those with a value."""
    while index < len(words) and words[index].startswith("-"):
        done = words[index] == "--"
        index += 2 if words[index] in takes else 1
        if done:
            break
    return index


def strip_wrappers(words: list[str]) -> tuple[list[str], Mapping[str, str]]:
    """The command without leading assignments and wrappers, and the assignments.

    ``command -v`` and ``command -V`` look a name up and run nothing, so they
    return no words.
    """
    assigned: dict[str, str] = {}
    index = 0
    while index < len(words):
        word = words[index]
        name = Path(word).name
        if ASSIGNMENT.match(word):
            key, _, value = word.partition("=")
            assigned[key] = value
            index += 1
        elif name in WRAPPERS:
            if name == "command" and words[index + 1 : index + 2] in (["-v"], ["-V"]):
                return [], assigned
            index = skip_options(words, index + 1, WRAPPERS[name])
        else:
            break
    return words[index:], assigned


def options(program: str, args: list[str]) -> list[tuple[str, str]]:
    """The (letter, value) pairs of the ssh-style options before the first operand."""
    takes = TAKES_VALUE[program]
    found: list[tuple[str, str]] = []
    index = 0
    while index < len(args) and args[index].startswith("-") and args[index] != "-":
        word = args[index]
        index += 1
        if word == "--":
            break
        for position, letter in enumerate(word[1:], start=1):
            if letter in takes:
                value = word[position + 1 :]
                if not value and index < len(args):
                    value = args[index]
                    index += 1
                found.append((letter, value))
                break
    return found


def avoids_agent(program: str, args: list[str]) -> bool:
    """Whether the first IdentityAgent option is none and an -i key is given."""
    pairs = options(program, args)
    matches = [AGENT_OPTION.match(value) for letter, value in pairs if letter == "o"]
    agents = [match[1] for match in matches if match]
    first = agents[0].strip("\"'").lower() if agents else ""
    return first == "none" and any(letter == "i" for letter, _ in pairs)


def rsh_of(args: list[str], assigned: Mapping[str, str]) -> str | None:
    """The remote shell command rsync is given, or None for the default."""
    for index, word in enumerate(args):
        if word.startswith("--rsh="):
            return word.removeprefix("--rsh=")
        if word == "--rsh" and index + 1 < len(args):
            return args[index + 1]
        if word.startswith("-") and not word.startswith("--") and "e" in word:
            tail = word.partition("e")[2]
            if tail:
                return tail
            if index + 1 < len(args):
                return args[index + 1]
    return assigned.get("RSYNC_RSH")


def has_ssh_operand(args: list[str]) -> bool:
    """Whether an rsync operand is a host:path over ssh, not a local path or daemon."""
    for word in args:
        if word.startswith(("-", "/", ".")) or "://" in word or "::" in word:
            continue
        if ":" in word:
            return True
    return False


def rsync_reason(args: list[str], assigned: Mapping[str, str]) -> str | None:
    """The deny reason for an rsync call that runs over ssh, else None."""
    if not has_ssh_operand(args):
        return None
    rsh = rsh_of(args, assigned)
    if rsh is None:
        return REASON_AGENT
    words, _ = strip_wrappers(shlex.split(rsh))
    if not words or Path(words[0]).name != "ssh":
        return None
    return None if avoids_agent("ssh", words[1:]) else REASON_AGENT


def names_ssh_dir(word: str) -> bool:
    """Whether a path word names the ssh directory or a path below it, not sessions."""
    home = str(Path.home())
    for part in word.split("="):
        relative = Path.cwd() == Path.home() and part.startswith(f"{SSH_DIR}")
        for base in (
            f"~/{SSH_DIR}",
            f"$HOME/{SSH_DIR}",
            f"${{HOME}}/{SSH_DIR}",
            f"{home}/{SSH_DIR}",
            *([SSH_DIR] if relative else []),
        ):
            sessions = f"{base}/sessions"
            if part == sessions or part.startswith(sessions + "/"):
                continue
            if part == base or part.startswith(base + "/"):
                return True
    return False


def inner_commands(text: str) -> list[str]:
    """The command texts inside $(...) and backticks in TEXT."""
    return [a or b for a, b in SUBSTITUTION.findall(text)]


def command_reason(words: list[str]) -> str | None:
    """The deny reason for one simple command, or None to allow it."""
    args, assigned = strip_wrappers(words)
    program = Path(args[0]).name if args else ""
    if program not in PRINTERS and any(names_ssh_dir(word) for word in words):
        return REASON_DIR
    if program == "ssh-add":
        return REASON_ADD
    if program in TAKES_VALUE:
        return None if avoids_agent(program, args[1:]) else REASON_AGENT
    if program == "rsync":
        return rsync_reason(args[1:], assigned)
    if program in SHELLS and "-c" in args[1:-1]:
        return verdict(args[args.index("-c") + 1])
    return None


def verdict(text: str) -> str | None:
    """The deny reason for a command, or None to allow it.

    An unbalanced quote makes the text unreadable, which allows it.
    """
    try:
        parsed = commands(text)
    except ValueError:
        return None
    for words in parsed:
        reason = command_reason(words)
        if reason:
            return reason
    for inner in inner_commands(text):
        reason = verdict(inner)
        if reason:
            return reason
    return None


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        payload = command_of(read_hook_stdin())
        reason = verdict(payload[0])
        if reason:
            print(json.dumps(decision(reason, antigravity=payload[1])))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
