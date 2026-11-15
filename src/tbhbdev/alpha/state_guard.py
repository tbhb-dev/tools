# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha state-guard`: block sign-off commits and unchecked open items.

A Claude Code hook for PreToolUse (Bash, Write and Edit) and SessionStart. It
reads the hook payload on stdin.

- PreToolUse on Bash blocks a `git commit` that adds a Signed-off-by or
  Co-authored-by trailer visible in its arguments, or the `-s` or `--signoff`
  flag. Heredoc and `-F` file message bodies pass this command check; the
  commit-msg identity check catches trailers there. The check needs no user
  config.
- SessionStart prints `[harness] rule` from the user config (see
  tbhbdev.userconfig), which Claude Code adds to the model's context. Without
  that key it prints nothing.
- PreToolUse on Write and Edit, when both `[harness] open_note` and
  `[harness] decisions_dir` are set: a file whose path ends in `open_note`
  can't gain a `- **item**` line unless the text holds `Decisions checked:`,
  and the block names `decisions_dir`. Without either key it does nothing.

The guard doesn't judge status claims in replies or messages. It once blocked
or tagged claims without evidence in a final reply (Stop hook) and in
SendMessage; the operator's 2026-11-13 decision removed both checks.

A command is a `git commit` only when one of its simple commands is one. The
scan drops heredoc bodies and splits shell commands outside quotes. It also
checks shell `-c` scripts and substitutions inside double quotes. Single
quotes hold literal text. Before matching `git commit`, it skips command
wrappers, environment assignments and options, and Git global option values.

The hook never blocks for lack of config, and a crash or invalid config
allows the call: a broken hook must not stop the agent's work.
"""

import re
import shlex
import sys

from tbhbdev import userconfig
from tbhbdev.alpha.repo import read_hook_stdin

type Json = dict[str, object]

TABLE = "harness"

TRAILER = re.compile(r"(?i)(signed-off-by|co-authored-by)\s*:")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z0-9_]+))")
OPEN_ITEM = re.compile(r"(?m)^- \*\*.*$")
SEPARATORS = frozenset("&|;\n(){}`")
GIT_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
)
WRAPPERS = frozenset({"env", "command", "exec"})
VALUE_FLAGS = frozenset(
    {
        "--message",
        "--file",
        "--template",
        "--reuse-message",
        "--reedit-message",
        "--author",
        "--date",
        "--cleanup",
        "--trailer",
        "--fixup",
        "--squash",
        "--pathspec-from-file",
    }
)
SHORT_VALUES = frozenset("mFCctU")
SHORT_OPTIONAL_VALUES = frozenset("Su")


def without_heredocs(command: str) -> str:
    """COMMAND without its heredoc bodies, which are data, not commands."""
    kept: list[str] = []
    ends: list[str] = []  # pragma: no mutate -- None is also false before assignment
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        kept.append(line)
        ends = [
            next(g for g in m.groups() if g is not None) for m in HEREDOC.finditer(line)
        ]
    return "\n".join(kept)


def shell_quote(quote: str, char: str) -> str:
    """The quote state after CHAR opens, closes or continues a quoted word."""
    if quote:
        return "" if char == quote else quote
    return char


def quoted_substitution(command: str, start: int) -> tuple[str, int]:
    """The script and next position of a substitution inside double quotes."""
    backtick = command[start] == "`"
    begin = start + (1 if backtick else 2)
    depth = 1
    quote = ""  # pragma: no mutate -- None has the same initial quote state
    i = begin
    while i < len(command):
        char = command[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if quote or char in "'\"":
            quote = shell_quote(quote, char)
        elif char == "`" and backtick:
            return command[begin:i], i + 1
        elif not backtick:
            depth += (char == "(") - (char == ")")
            if depth == 0:
                return command[begin:i], i + 1
        i += 1
    return command[begin:], i


def simple_commands(command: str) -> list[str]:
    """COMMAND split at the separators that sit outside quotes."""
    found: list[str] = []
    current: list[str] = []
    quote = ""  # pragma: no mutate -- None has the same initial quote state
    i = 0
    while i < len(command):
        char = command[i]
        if char == "\\" and quote != "'":
            current.append(command[i : i + 2])
            i += 2
            continue
        if quote == '"' and (char == "`" or command.startswith("$(", i)):
            script, i = quoted_substitution(command, i)
            found.extend(simple_commands(script))
            continue
        if quote or char in "'\"":
            quote = shell_quote(quote, char)
            current.append(char)
        elif char in SEPARATORS:
            found.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    found.append("".join(current))
    return found


def unwrap_command(words: list[str]) -> list[str]:
    """Skip command wrappers, env options and environment assignments."""
    while words:
        word = words[0]
        if word == "env":
            words = words[1:]
            while words and words[0].startswith("-"):
                option = words[0]
                words = (
                    words[2:]
                    if option in {"-u", "--unset", "-C", "--chdir"}
                    else words[1:]
                )
        elif word in WRAPPERS or ASSIGNMENT.match(word):
            words = words[1:]
        else:
            break
    return words


def shell_script(simple: str) -> str | None:
    """The script argument of a shell's -c invocation, if present."""
    try:
        words = unwrap_command(shlex.split(simple))
    except ValueError:
        return None
    if not words or words[0].split("/")[-1] not in {"bash", "sh", "zsh"}:
        return None
    for index, word in enumerate(words[1:], start=1):
        if word == "-c" and index + 1 < len(words):
            return words[index + 1]
    return None


def commit_arguments(simple: str) -> list[str] | None:
    """The arguments after `commit` when SIMPLE is a `git commit`, else None."""
    try:
        words = shlex.split(simple)
    except ValueError:
        return None
    words = unwrap_command(words)
    if not words or words[0].split("/")[-1] != "git":
        return None
    rest = words[1:]
    while rest and (rest[0] in GIT_OPTIONS or rest[0].startswith("--")):
        rest = rest[2:] if rest[0] in GIT_OPTIONS else rest[1:]
    if not rest or rest[0] != "commit":
        return None
    return rest[1:]


def short_signoff(word: str) -> tuple[bool, bool]:
    """Whether WORD signs off, and whether its next argument is a value."""
    if not word.startswith("-") or word.startswith("--"):
        return False, False
    for index, flag in enumerate(word[1:], start=1):
        if flag == "s":
            return True, False
        if flag in SHORT_VALUES:
            return False, index == len(word) - 1
        if flag in SHORT_OPTIONAL_VALUES:
            return False, False
    return False, False


def signs_off(arguments: list[str]) -> bool:
    """Whether a `git commit`'s ARGUMENTS add a sign-off or co-author trailer."""
    if TRAILER.search(" ".join(arguments)):
        return True
    skip = False  # pragma: no mutate -- None also means no value to skip
    for word in arguments:
        if skip:
            skip = False  # pragma: no mutate -- None also means no value to skip
            continue
        if word == "--signoff":
            return True
        signed, skip = short_signoff(word)
        if signed:
            return True
        skip = skip or word in VALUE_FLAGS
    return False


def commit_trailer_reason(command: str) -> str | None:
    """Why COMMAND, a `git commit` that adds a sign-off trailer, is blocked."""
    for simple in simple_commands(without_heredocs(command)):
        script = shell_script(simple)
        if script is not None:
            reason = commit_trailer_reason(script)
            if reason:
                return reason
        arguments = commit_arguments(simple)
        if arguments is not None and signs_off(arguments):
            return (
                "state-guard: agent commits carry no Signed-off-by or "
                "Co-authored-by trailer and no -s or --signoff. Remove it."
            )
    return None


def open_note_reason(tool: str, tool_input: Json) -> str | None:
    """Why a Write or Edit of the open note that adds an unchecked item is blocked."""
    open_note = userconfig.optional_string(TABLE, "open_note")
    decisions = userconfig.optional_string(TABLE, "decisions_dir")
    path = str(tool_input.get("file_path", ""))
    if not open_note or not decisions or not path.endswith(open_note):
        return None
    new = tool_input.get("content" if tool == "Write" else "new_string")
    text = str(new)
    old = str(tool_input.get("old_string")) if tool == "Edit" else str(None)
    added = [item for item in OPEN_ITEM.findall(text) if item not in old]
    if added and "Decisions checked:" not in text:
        return (
            "state-guard: a new open item needs 'Decisions checked: <grep "
            f"terms>', showing you searched {decisions} first."
        )
    return None


def pre_tool_reason(data: Json) -> str | None:
    """Why the PreToolUse call DATA describes is blocked, or None."""
    tool = data.get("tool_name")
    raw = data.get("tool_input")
    tool_input: Json = raw if isinstance(raw, dict) else {}
    if tool == "Bash":
        return commit_trailer_reason(str(tool_input.get("command", "")))
    if tool in {"Write", "Edit"}:
        return open_note_reason(str(tool), tool_input)
    return None


def run(data: Json) -> int:
    """Handle one hook payload; the exit status."""
    if data.get("hook_event_name") == "SessionStart":
        rule = userconfig.optional_string(TABLE, "rule")
        if rule:
            print(rule)
        return 0
    reason = pre_tool_reason(data)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


def main() -> int:
    """Read the payload on stdin and run it; never fail for a crash or bad config."""
    try:
        return run(read_hook_stdin())
    except userconfig.ConfigError as exc:
        print(f"state-guard: ignoring the config: {exc}", file=sys.stderr)
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
