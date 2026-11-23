# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-agent`: keep agent commands off the SSH agent.

This PreToolUse hook shares guard-gh-pr's payload contracts but parses the
command with its own narrow shlex pass. It splits the command at shell
operators, strips `env`, `sudo`, `command`, and similar wrappers, and denies:

- `ssh-add` as a command word;
- a path argument of a shell command, or the `file_path` or `path` of another
  tool such as Read, that names the operator's ssh directory in their home,
  except its `sessions` subdirectory.

It does not check ssh, scp, sftp, or rsync arguments. Text that only mentions a
variable or a path, such as a commit message, is never matched. Like the other
guards, allowing prints nothing and unreadable input or a crash fails open.

Known gaps: a program name held in a variable, runners such as `timeout` and
`xargs`, commands inside `$(...)`, backticks, and `bash -c`, a heredoc body
that starts a line with a program name, and Antigravity file tools.
"""

import json
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.guard_gh_pr import command_of, decision
from tbhbdev.alpha.repo import read_hook_stdin

if TYPE_CHECKING:
    from tbhbdev.alpha.guard_gh_pr import Json

REASON_ADD = "ssh-add is blocked: agent sessions never load keys into an agent."
REASON_DIR = (
    "The call is blocked: it names a path in the operator's ssh directory "
    "outside its sessions subdirectory."
)
SSH_DIR = ".ssh"
OPERATORS = ";&|()<>\n"
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
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
PRINTERS = {"echo", "printf"}
PATH_FIELDS = ("file_path", "path")


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


def strip_wrappers(words: list[str]) -> list[str]:
    """The command without its leading assignments and wrappers."""
    index = 0
    while index < len(words):
        word = words[index]
        name = Path(word).name
        if ASSIGNMENT.match(word):
            index += 1
        elif name in WRAPPERS:
            index = skip_options(words, index + 1, WRAPPERS[name])
        else:
            break
    return words[index:]


def names_ssh_dir(word: str) -> bool:
    """Whether a path word names the ssh directory or a path below it, not sessions.

    A word with whitespace is text, such as a commit message, not a path.
    """
    if any(char.isspace() for char in word):
        return False
    home = str(Path.home())
    for part in word.split("="):
        relative = Path.cwd() == Path.home() and part.startswith(SSH_DIR)
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


def command_reason(words: list[str]) -> str | None:
    """The deny reason for one simple command, or None to allow it."""
    args = strip_wrappers(words)
    program = Path(args[0]).name if args else ""
    if program == "ssh-add":
        return REASON_ADD
    if program not in PRINTERS and any(names_ssh_dir(word) for word in words):
        return REASON_DIR
    return None


def verdict(text: str) -> str | None:
    """The deny reason for a shell command, or None to allow it.

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
    return None


def path_reason(data: Json) -> str | None:
    """The deny reason for a path field of the tool input, or None."""
    inputs = data.get("tool_input")
    if not isinstance(inputs, dict):
        return None
    for field in PATH_FIELDS:
        value = inputs.get(field)
        if isinstance(value, str) and names_ssh_dir(value):
            return REASON_DIR
    return None


def denial() -> str | None:
    """The deny decision as JSON for the payload on stdin, or None to allow."""
    data = read_hook_stdin()
    text, antigravity = command_of(data)
    reason = verdict(text) or path_reason(data)
    return json.dumps(decision(reason, antigravity=antigravity)) if reason else None


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        blocked = denial()
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    if blocked:
        print(blocked)
    return 0
