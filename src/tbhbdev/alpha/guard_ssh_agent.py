# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-agent`: keep agent commands off the SSH agent.

This PreToolUse hook shares guard-gh-pr's payload contracts but reads the
command with its own small shell lexer. It denies only two shapes:

- `ssh-add` as the first word of a simple command, after plain `NAME=value`
  assignments and the wrappers `command`, `exec`, `nohup`, `time` and `env`
  used without options;
- the `file_path` or `path` of another tool such as Read that resolves, with
  symlinks and `..` resolved against the payload's working directory, into the
  operator's ssh directory except its `sessions` subdirectory.

Every other shape is allowed, including a command with a redirection, heredoc,
`case`, group, parenthesis, `[[`, a `${` expansion, command substitution or
backtick, a wrapper with an option, a function and anything else the lexer
does not read. Like the
other guards, allowing prints nothing and unreadable input or a crash,
including a closed output pipe, fails open.

Known gaps, all false allows: every wrapped or unparsed form above, `sudo` or
`doas`, a program name held in a variable, runners such as `timeout`, `xargs`
and `bash -c`, a keyword such as `then` before `ssh-add`, any shell word that
names the ssh directory, a path that differs from the ssh directory only in
case on a case-insensitive volume, and the Antigravity file tools and
`workspacePaths`.
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
UNPARSED = re.compile(r"[()`<>]|\$\{|\$'|\[\[|\bcase\b")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"command", "exec", "nohup", "time", "env"}
SYSTEM_DIRS = {
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
}
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


def command_word(words: list[str]) -> str:
    """The first word after leading assignments and option-less wrappers.

    A wrapper is a bare name or a path in a system directory, never a relative
    path such as `./time` or a project script such as `/home/u/tools/env`. Any
    option after a wrapper means the command is not read.
    """
    wrapper = ""
    for word in words:
        if wrapper and word.startswith("-"):
            return ""  # a wrapper with an option is not one we read
        name = Path(word).name
        if not (
            ASSIGNMENT.match(word)
            or (
                name in WRAPPERS
                and ("/" not in word or str(Path(word).parent) in SYSTEM_DIRS)
            )
        ):
            return name
        wrapper = name
    return ""


def expand(text: str, home: str) -> str:
    """TEXT with a leading `~` or `$HOME` expanded."""
    for prefix in ("~", "$HOME", "${HOME}"):
        if text == prefix or text.startswith(prefix + "/"):
            return home + text[len(prefix) :]
    return text


def in_sessions(path: str, ssh: str, sessions: str) -> bool:
    """Whether PATH, already below SSH, is in the sessions directory.

    The first component is compared by file identity too, so `SESSIONS` names
    the same directory on a case-insensitive volume.
    """
    if path == sessions or path.startswith(sessions + "/"):
        return True
    first = path[len(ssh) + 1 :].split("/", 1)[0]
    try:
        return Path(f"{ssh}/{first}").samefile(sessions)
    except OSError:
        return False


def under_ssh(path: str, home: str) -> bool:
    """Whether PATH is the ssh directory or below it, outside sessions."""
    ssh = os.path.realpath(f"{home}/{SSH_DIR}")
    if path != ssh and not path.startswith(ssh + "/"):
        return False
    return not in_sessions(path, ssh, os.path.realpath(f"{ssh}/sessions"))


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

    An unbalanced quote makes the text unreadable, which allows it.
    """
    try:
        parsed = segments(text)
    except ValueError:
        return None
    return REASON_ADD if any(command_word(w) == "ssh-add" for w in parsed) else None


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
