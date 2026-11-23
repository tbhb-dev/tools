# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-agent`: keep agent commands off the SSH agent.

This PreToolUse hook shares guard-gh-pr's payload contracts but reads the
command with its own small shell lexer. It decides only what the lexer can
read reliably. The hook denies:

- `ssh-add` as the command word of a simple command, which is the first word
  after an operator or the start of the text, past assignments, wrappers such
  as `env` and `sudo`, and keywords such as `then`;
- a plain word of a shell command, or the `file_path` or `path` of another
  tool such as Read, that resolves, against the payload's working directory
  and with symlinks and `..` resolved, into the operator's ssh directory
  except its `sessions` subdirectory. This covers ssh, scp, sftp and rsync
  arguments too.

A word is plain when it has no quote or escape. `~` and `$HOME` expand in a
plain word, and a name that starts with `--option=` or `NAME=` is read after
the `=`. The hook never looks inside heredoc bodies, comments, quoted text,
redirection operands, here-strings or `case` patterns. Like the other guards,
allowing prints nothing and unreadable input or a crash, including a closed
output pipe, fails open.

Known gaps, all false allows: a path with any quote or escape, a redirection
operand, a bare new filename in the ssh directory, a program name held in a
variable, runners such as `timeout` and `xargs`, commands inside `$(...)`,
backticks, and `bash -c`, a heredoc that starts inside such a command, and
the Antigravity file tools and `workspacePaths`.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

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
OPERATORS = ";&|()<>"
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
PREFIX = re.compile(r"(?:--?[A-Za-z][\w-]*|[A-Za-z_]\w*)=")
KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{"}
# Wrapper programs and the options of each that take a value.
WRAPPERS = {
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user"}
    | {"--group", "--host", "--prompt", "--chdir"},
    "command": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": set(),
    "nice": {"-n"},
}
LOOKUP = re.compile(r"-[A-Za-z]*[vV][A-Za-z]*")
PRINTERS = {"echo", "printf"}
PATH_FIELDS = ("file_path", "path")


class Word(NamedTuple):
    """A shell word, plain when it has no quote or escape."""

    text: str
    plain: bool


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
        self.found: list[list[Word]] = [[]]
        self.buf: list[str] = []
        self.quoted = False  # the word has a quote or escape
        self.started = False
        self.depth = 0  # open parentheses
        self.drop = False  # the next word is a redirection operand
        self.heredoc = False  # the next word ends a heredoc body
        self.ends: list[str] = []  # terminators of the bodies after this line

    def flush(self) -> None:
        """End the word being built."""
        if self.started:
            word = "".join(self.buf)
            if self.heredoc:
                self.ends.append(word.removeprefix("-"))
            elif not self.drop:
                self.found[-1].append(Word(word, not self.quoted))
            self.drop = self.heredoc = False
        self.buf.clear()
        self.quoted = self.started = False

    def quote(self, mark: str) -> None:
        """Read the quoted part at the index."""
        part, self.index = quoted(self.text, self.index, mark)
        self.buf.append(part)
        self.quoted = self.started = True

    def escape(self) -> None:
        """Read a backslash and the character it escapes."""
        following = self.text[self.index + 1 : self.index + 2]
        self.index += 2
        if following != "\n":
            self.buf.append(following)
            self.quoted = self.started = True

    def skip_bodies(self) -> None:
        """Skip the heredoc bodies that start on the line just ended."""
        for end in self.ends:
            while self.index < len(self.text):
                stop = self.text.find("\n", self.index) % (len(self.text) + 1)
                line = self.text[self.index : stop]
                self.index = min(stop + 1, len(self.text))
                if line.strip() == end:
                    break
        self.ends.clear()

    def operator(self) -> None:
        """Read the operator at the index, which starts the next segment."""
        self.flush()
        end = self.index + 1
        newline = self.text[self.index] == "\n"
        if not newline:
            while end < len(self.text) and self.text[end] in OPERATORS:
                end += 1
        run = self.text[self.index : end]
        for char in run:
            if char == ")" and not self.depth:
                self.found[-1].clear()  # a `case` pattern, not a command
            self.depth += (char == "(") - (char == ")" and self.depth > 0)
        self.index = end
        if set(run) <= set("<>"):
            # A redirection's operand is neither a command nor a path.
            self.drop = True
            self.heredoc = run == "<<"
        else:
            self.found.append([])
        if newline:
            self.skip_bodies()

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


def segments(text: str) -> list[list[Word]]:
    """The simple commands of TEXT, split at the shell operators outside quotes.

    Raises:
        ValueError: TEXT has an unbalanced quote.
    """
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
                LOOKUP.fullmatch(word) for word in words[index + 1 : end]
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


def names_ssh_dir(word: Word, cwd: str) -> bool:
    """Whether a plain word names the ssh directory or a path below it."""
    home = str(Path.home())
    text = word.text
    found = PREFIX.match(text)
    if found:
        text = text[found.end() :]
    if not word.plain or not text or text.startswith("-"):
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


def segment_reason(words: list[Word], cwd: str) -> str | None:
    """The deny reason for one simple command, or None to allow it."""
    args = strip_wrappers([word.text for word in words])
    program = Path(args[0]).name if args else ""
    if program == "ssh-add":
        return REASON_ADD
    if program not in PRINTERS and any(names_ssh_dir(w, cwd) for w in words):
        return REASON_DIR
    return None


def verdict(text: str, cwd: str | None = None) -> str | None:
    """The deny reason for a shell command, or None to allow it.

    CWD is the working directory, which defaults to the process's. An unbalanced
    quote makes the text unreadable, which allows it.
    """
    try:
        parsed = segments(text)
    except ValueError:
        return None
    for words in parsed:
        reason = segment_reason(words, cwd or str(Path.cwd()))
        if reason:
            return reason
    return None


def path_reason(data: Json, cwd: str | None = None) -> str | None:
    """The deny reason for a path field of the tool input, or None."""
    inputs = data.get("tool_input")
    if not isinstance(inputs, dict):
        return None
    for field in PATH_FIELDS:
        value = inputs.get(field)
        if isinstance(value, str) and names_ssh_dir(
            Word(value, plain=True), cwd or str(Path.cwd())
        ):
            return REASON_DIR
    return None


def denial() -> str | None:
    """The deny decision as JSON for the payload on stdin, or None to allow."""
    data = read_hook_stdin()
    text, antigravity = command_of(data)
    cwd = data.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None
    reason = verdict(text, cwd) or path_reason(data, cwd)
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
