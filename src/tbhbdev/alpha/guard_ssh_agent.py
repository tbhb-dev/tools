# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-agent`: keep agent commands off the SSH agent.

This PreToolUse hook shares guard-gh-pr's payload contracts but reads the
command with its own small shell lexer. It decides only what the lexer can
read reliably. The hook denies:

- `ssh-add` as the command word of a simple command, which is the first word
  after an operator or the start of the text, past assignments, wrappers such
  as `env` and keywords such as `then`;
- the `file_path` or `path` of another tool such as Read that resolves, with
  symlinks and `..` resolved against the payload's working directory, into the
  operator's ssh directory except its `sessions` subdirectory.

The hook never looks inside comments or quoted text. A command with a
redirection operator, a heredoc or here-string, a `case`, a group or
parenthesis, a command substitution, a backtick, or `sudo` or `doas` as a
command word is not parsed and is allowed whole. Like the other guards,
allowing prints nothing and unreadable input or a crash, including a closed
output pipe, fails open.

Known gaps, all false allows: every unparsed form above, any shell word that
names the ssh directory, a program name held in a variable, runners such as
`timeout`, `xargs`, `env -S` and `bash -c`, `builtin command ssh-add`, a path
that differs from the ssh directory only in case on a case-insensitive volume,
and the Antigravity file tools and `workspacePaths`.
"""

import json
import os
import re
import sys
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
OPERATORS = ";&|"
# Constructs the lexer does not handle: the whole command is allowed.
UNPARSED = re.compile(r"[()`<>]|\bcase\b")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{"}
# Wrapper programs and the options of each that take a value.
WRAPPERS = {
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "command": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": set(),
    "nice": {"-n"},
}
LOOKUP = re.compile(r"-[A-Za-z]*[vV][A-Za-z]*")
ELEVATORS = {"sudo", "doas"}
PATH_FIELDS = ("file_path", "path")


def quoted(text: str, index: int, mark: str) -> tuple[str, int]:
    """The text up to the MARK closing the quote at INDEX and the index after it.

    Raises:
        ValueError: the quote is not closed.
    """
    out: list[str] = []
    index += 1
    while index < len(text) and text[index] != mark:
        if (
            mark == '"'
            and text[index] == "\\"
            and text[index + 1 : index + 2] in {*'"\\$`'}
        ):
            index += 1
        out.append(text[index])
        index += 1
    if index >= len(text):
        raise ValueError(mark)
    return "".join(out), index + 1


class Lexer:
    """Reads shell text into segments, tracking the word being built."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0
        self.found: list[list[str]] = [[]]
        self.buf: list[str] = []
        self.started = False

    def flush(self) -> None:
        """End the word being built."""
        if self.started:
            self.found[-1].append("".join(self.buf))
        self.buf.clear()
        self.started = False

    def quote(self, mark: str) -> None:
        """Read the quoted part at the index."""
        part, self.index = quoted(self.text, self.index, mark)
        self.buf.append(part)
        self.started = True

    def escape(self) -> None:
        """Read a backslash and the character it escapes."""
        following = self.text[self.index + 1 : self.index + 2]
        self.index += 2
        if following != "\n":
            self.buf.append(following)
            self.started = True

    def operator(self) -> None:
        """Read the operator at the index, which starts the next segment."""
        self.flush()
        self.index += 1
        self.found.append([])

    def step(self) -> None:
        """Read one character or construct at the index."""
        char = self.text[self.index]
        if char in "'\"":
            self.quote(char)
        elif char == "\\":
            self.escape()
        elif char == "#" and not self.started:
            self.index = self.text.find("\n", self.index) % (len(self.text) + 1)
        elif char in " \t\r":
            self.flush()
            self.index += 1
        elif char == "\n" or char in OPERATORS:
            self.operator()
        else:
            self.buf.append(char)
            self.started = True
            self.index += 1


def segments(text: str) -> list[list[str]]:
    """The simple commands of TEXT, split at the shell operators outside quotes.

    Raises:
        ValueError: TEXT has an unbalanced quote.
    """
    if UNPARSED.search(text):
        return []
    lexer = Lexer(text)
    while lexer.index < len(text):
        lexer.step()
    lexer.flush()
    return [words for words in lexer.found if words]


def skip_options(words: list[str], index: int, takes: set[str]) -> int:
    """The index after the options at INDEX, where TAKES lists those with a value."""
    while index < len(words) and words[index].startswith("-"):
        done = words[index] == "--"
        index += 2 if words[index] in takes else 1
        if done:
            break
    return index


def strip_wrappers(words: list[str]) -> list[str]:
    """The command without its leading assignments, keywords and wrappers.

    `command -v` and `command -V` only look a name up, so they return nothing.
    """
    index = 0
    while index < len(words):
        word = words[index]
        name = Path(word).name
        if ASSIGNMENT.match(word) or word in KEYWORDS:
            index += 1
        elif name in WRAPPERS:
            end = skip_options(words, index + 1, WRAPPERS[name])
            if name == "command" and any(
                LOOKUP.fullmatch(w) for w in words[index + 1 : end]
            ):
                return []
            index = end
        else:
            break
    return words[index:]


def expand(text: str, home: str) -> str:
    """TEXT with a leading `~` or `$HOME` expanded."""
    for prefix in ("~", "$HOME", "${HOME}"):
        if text == prefix or text.startswith(prefix + "/"):
            return home + text[len(prefix) :]
    return text


def under_ssh(path: str, home: str) -> bool:
    """Whether PATH is the ssh directory or below it, outside sessions."""
    ssh = os.path.realpath(f"{home}/{SSH_DIR}")
    sessions = f"{ssh}/sessions"
    if path == sessions or path.startswith(sessions + "/"):
        return False
    return path == ssh or path.startswith(ssh + "/")


def names_ssh_dir(text: str, cwd: str) -> bool:
    """Whether a path names the ssh directory or a path below it."""
    home = str(Path.home())
    if not text or text.startswith("-"):
        return False
    text = expand(text, home)
    if not Path(text).is_absolute():
        full = str(Path(cwd) / text)
        # A bare name is a path only if it exists, so `git log main` is allowed.
        if (
            "/" not in text
            and not text.startswith(".")
            and not Path(full).is_symlink()
            and not Path(full).exists()
        ):
            return False
        text = full
    return under_ssh(os.path.realpath(text), home)


def verdict(text: str) -> str | None:
    """The deny reason for a shell command, or None to allow it.

    An unbalanced quote makes the text unreadable, which allows it. So does
    `sudo` or `doas` as a command word, whose arguments the hook can't read.
    """
    try:
        parsed = segments(text)
    except ValueError:
        return None
    programs = [
        Path(args[0]).name for words in parsed if (args := strip_wrappers(words))
    ]
    if ELEVATORS.intersection(programs):
        return None
    return REASON_ADD if "ssh-add" in programs else None


def path_reason(data: Json, cwd: str | None = None) -> str | None:
    """The deny reason for a path field of the tool input, or None."""
    inputs = data.get("tool_input")
    if not isinstance(inputs, dict):
        return None
    for field in PATH_FIELDS:
        value = inputs.get(field)
        if isinstance(value, str) and names_ssh_dir(value, cwd or str(Path.cwd())):
            return REASON_DIR
    return None


def denial() -> str | None:
    """The deny decision as JSON for the payload on stdin, or None to allow."""
    data = read_hook_stdin()
    text, antigravity = command_of(data)
    cwd = data.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None
    reason = verdict(text) or path_reason(data, cwd)
    return json.dumps(decision(reason, antigravity=antigravity)) if reason else None


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        blocked = denial()
        if blocked:
            print(blocked)
            sys.stdout.flush()
    except Exception:  # noqa: BLE001  # fail open on any crash, even a closed pipe
        return 0
    return 0
