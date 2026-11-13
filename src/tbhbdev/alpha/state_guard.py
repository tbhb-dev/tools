# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha state-guard`: mark status claims that carry no evidence.

A Claude Code hook for PreToolUse (Bash, SendMessage, Write and Edit), Stop and
SessionStart. It reads the hook payload on stdin.

- PreToolUse on SendMessage inserts `[state-guard: unverified]` before each
  status sentence without evidence, emits updatedInput and logs each sentence.
  Only `[state_guard] tag_messages = true` turns this on. Without it, or with
  any other value, a bad config or another hook event, the guard refuses with
  exit 2.
  Sender-written tags cannot replace the guard's own tag.
  On Bash it blocks a `git commit` that adds a Signed-off-by or
  Co-authored-by trailer visible in its arguments, or the `-s` or `--signoff`
  flag. Heredoc and `-F` file message bodies pass this command check; the
  commit-msg identity check catches trailers there. Neither check needs the
  user config.
- Stop blocks a final reply with the same claims by printing
  `{"decision": "block", ...}`, asking for only the corrected sentences as a
  short follow-up. A Stop that the hook itself caused
  (`stop_hook_active`) passes, so the model can't loop.
- SessionStart prints `[harness] rule` from the user config (see
  tbhbdev.userconfig), which Claude Code adds to the model's context. Without
  that key it prints nothing.
- PreToolUse on Write and Edit, when both `[harness] open_note` and
  `[harness] decisions_dir` are set: a file whose path ends in `open_note`
  can't gain a `- **item**` line unless the text holds `Decisions checked:`,
  and the block names `decisions_dir`. Without either key it does nothing.

Evidence is a git sha (7 to 40 hex characters, with a digit and a letter), a
PR or issue number, a log path, inline code, "output" or "read-back", or
attribution ("says", "said", "reports"). A claim marked "unverified", or "not
done", also passes.

A command is a `git commit` only when one of its simple commands is one. The
scan drops heredoc bodies and splits shell commands outside quotes. It also
checks shell `-c` scripts and substitutions inside double quotes. Single
quotes hold literal text. Before matching `git commit`, it skips command
wrappers, environment assignments and options, and Git global option values.

The hook never blocks for lack of config, and a crash or invalid config
allows the call: a broken hook must not stop the agent's work. The one
exception is a failure while tagging a SendMessage, which keeps the refusal.
"""

import io
import json
import os
import re
import shlex
import stat
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
lazy from collections.abc import Callable

from tbhbdev import userconfig
from tbhbdev.alpha.agent.runfiles import state_home
from tbhbdev.alpha.repo import read_hook_stdin

type Json = dict[str, object]

TABLE = "harness"
TAG = "[state-guard: unverified]"
GAP = "\x00"  # stands for a removed tag until the text stops changing
# Every Unicode format character (category Cf), which renders as nothing: soft
# hyphen, Arabic marks, zero-width space and the other zero-width and bidi
# controls, BOM, the interlinear and tag characters.
FORMAT_RANGES = (
    (0x00AD, 0x00AD), (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD),
    (0x070F, 0x070F), (0x0890, 0x0891), (0x08E2, 0x08E2), (0x180E, 0x180E),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x206F),
    (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0x110BD, 0x110BD), (0x110CD, 0x110CD),
    (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)  # fmt: skip
FORMAT_CHARS = "".join(
    re.escape(chr(low)) + "-" + re.escape(chr(high)) for low, high in FORMAT_RANGES
)
# A gap is anything a reader sees as nothing or as space, line breaks included.
SPACES = rf"[\s\x00{FORMAT_CHARS}]*"
# A tag cut by tags or spaces, as in "[state-gu ard:", is still a tag fragment.
OPEN = r"\[" + SPACES + SPACES.join(re.escape(c) for c in "state-guard:")
BODY = rf"{OPEN}{SPACES}(?:unverified{SPACES})*"
CLOSED = re.compile(BODY + r"\]", re.IGNORECASE)
FRAGMENT = re.compile(BODY, re.IGNORECASE)
GAPS = re.compile(rf"[\s\x00{FORMAT_CHARS}]+")
CLAIM = re.compile(
    r"\b(?:is|are|was|were|now|already|has been|have been)\s+"
    r"(?:done|applied|merged|shipped|deployed|live|fixed|complete|completed|"
    r"installed|running|verified|in place|started)\b"
    r"|\b(?:shipped|merged|applied|deployed|landed(?!\s+on\b)|rolled out)\b",
    re.IGNORECASE,
)
EVIDENCE = re.compile(
    r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b|#\d+|\bsays\b|\bsaid\b|\breports?\b|\.log\b|`[^`]+`"
    r"|\boutput\b|\bread-back\b|\bunverified\b|\bnot (?:done|applied|merged)\b",
    re.IGNORECASE,
)
TRAILER = re.compile(r"(?i)(signed-off-by|co-authored-by)\s*:")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z0-9_]+))")
OPEN_ITEM = re.compile(r"(?m)^- \*\*.*$")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n")
SENTENCE_PARTS = re.compile(f"({SENTENCE_END.pattern})")
SENTENCE_PREFIX = re.compile(
    r"^\s*(?:(?:[-+*]|\d+[.)])\s+)?(?:\*\*[^*\n]+(?::\*\*|\*\*:)\s*)?"
)
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
MAX_QUOTED = 160
MAX_LISTED = 3
# Exemptions from the baseline claim judge. Each rule takes a sentence and one
# claim match in it and returns True to exempt that match. A rule is added in its
# own pull request, with its sentences listed in the differential test.
EXEMPTIONS: tuple[Callable[[str, re.Match[str]], bool], ...] = ()


def exempt(chunk: str, match: re.Match[str]) -> bool:
    """Whether any exemption rule exempts MATCH in CHUNK; the set is empty."""
    return any(rule(chunk, match) for rule in EXEMPTIONS)


def unsupported_claims(text: str) -> list[str]:
    """The sentences or lines of TEXT with a status claim and no evidence."""
    return [
        chunk.strip()[:MAX_QUOTED]
        for chunk in SENTENCE_END.split(text)
        if not EVIDENCE.search(chunk)
        and any(not exempt(chunk, match) for match in CLAIM.finditer(chunk))
    ]


def final_claim_reason(bad: list[str]) -> str:
    """The block message for BAD claims in a reply that was already shown."""
    offending = " | ".join(bad[:MAX_LISTED])
    return (
        "state-guard: your reply makes a status claim without evidence. "
        f"Offending: {offending}. Fix only these sentences (add the PR number "
        "and sha, the merge sha, command output or a log path, attribute it, "
        "or say it is unverified) and send just the corrected sentences; "
        "don't repeat the rest of the reply."
    )


def claim_reason(bad: list[str]) -> str:
    """The block message for BAD claims in a message that was not sent."""
    offending = " | ".join(bad[:MAX_LISTED])
    return (
        "state-guard: this message makes a status claim without evidence, so it "
        f"was not sent. Offending: {offending}. Resend the same message with only "
        "these sentences fixed (add the PR number and sha, the merge sha, "
        "command output or a log path, attribute it, or say it is unverified); "
        "leave the rest unchanged."
    )


def strip_tags(text: str, fill: str = " ") -> str:
    """Judge a message as if its sender-written tags weren't there.

    Each tag, and any leftover `[state-guard:` fragment with its bare "unverified"
    words and closing bracket, becomes FILL, in any case and with any whitespace or
    format character inside, so a sender can't leave an "unverified" behind as
    evidence. Closed tags go first, innermost out, so an outer opener keeps its own
    "unverified" until the fragment pass takes both. Removal repeats because a
    removal can join the text around it into a new tag. Then each run of spaces and
    format characters collapses to one space, except that line breaks stay because
    they end sentences.
    """
    while True:
        new = CLOSED.sub(GAP, text)
        if new == text:
            new = FRAGMENT.sub(GAP, text)
        if new == text:
            break
        text = new
    return GAPS.sub(lambda run: collapse(run.group(), fill), text)


def collapse(run: str, fill: str) -> str:
    """One gap run as it renders: a line break, a space, or FILL for hidden text."""
    if "\n" in run:
        return "\n"
    return " " if any(c.isspace() for c in run) else fill


def renderings(text: str) -> tuple[str, str]:
    """TEXT with tags replaced by a space, and with tags removed and words joined."""
    return strip_tags(text), strip_tags(text, "")


def judged_claims(text: str) -> list[str]:
    """Unsupported claims of TEXT in either rendering: evidence needs both.

    A tag inside a word splits it in one rendering and joins it in the other, as in
    "do[tag]ne" and "no[tag]t merged", so a claim in either one counts.
    """
    spaced, joined = renderings(text)
    found = unsupported_claims(spaced)
    return found + [claim for claim in unsupported_claims(joined) if claim not in found]


def tag_sentences(text: str) -> tuple[str, list[str]]:
    """Tag unsupported sentences while preserving their full text and separators."""
    parts = SENTENCE_PARTS.split(text)
    sentences: list[str] = []
    for index, chunk in enumerate(parts):
        if judged_claims(chunk):
            parts[index] = SENTENCE_PREFIX.sub(r"\g<0>" + TAG + " ", chunk)
            sentences.append(chunk.strip())
    return "".join(parts), sentences


def open_log(directory: Path) -> int:
    """A descriptor for the private audit log; refuses symlinks and special files.

    O_NOFOLLOW covers the last directory and the file only. A symlinked parent of
    the state directory still redirects the log, so the state home must be trusted.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    dir_fd = os.open(directory, flags)
    try:
        os.fchmod(dir_fd, 0o700)
        fd = os.open(
            "tagged.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=dir_fd,
        )
    finally:
        os.close(dir_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink > 1
        or info.st_uid != os.getuid()
    ):
        os.close(fd)
        message = "the audit log is not a single-link regular file owned by this user"
        raise OSError(message)
    return fd


def log_tagged(data: Json, tool_input: Json, sentences: list[str]) -> None:
    """Append private audit records; a logging failure must still send the message."""
    try:
        fd = open_log(state_home(Path.home()) / "tbhbdev" / "state-guard")
        with os.fdopen(fd, "ab") as out:
            os.fchmod(out.fileno(), 0o600)
            for sentence in sentences:
                record: Json = {
                    "time": datetime.now(UTC).isoformat(),
                    "session_id": data.get("session_id"),
                    "rule": next(
                        m.group()
                        for r in renderings(sentence)
                        for m in CLAIM.finditer(r)
                    ),
                    "sentence": sentence,
                }
                if "to" in tool_input:
                    record["recipient"] = tool_input["to"]
                out.write((json.dumps(record) + "\n").encode())
    except Exception:  # noqa: BLE001  # documented fail-open: the log isn't the verdict
        return


def tagging_enabled(data: Json) -> bool:
    """True only for PreToolUse with `[state_guard] tag_messages = true`.

    The config opens without blocking and is judged by its descriptor, so a path
    swapped for a named pipe after the check can't hang the hook.
    """
    if data.get("hook_event_name") != "PreToolUse":
        return False
    try:
        fd = os.open(userconfig.path(), os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            message = "the config is not a regular file"
            raise OSError(message)
        settings = tomllib.load(source).get("state_guard", {})
    if not isinstance(settings, dict):
        message = "[state_guard] is not a table"
        raise TypeError(message)
    return settings.get("tag_messages") is True


# The most bytes the hook writes to stdout, newline included. The smallest pipe
# a platform allows holds one 4096-byte page, so a write this size completes even
# when the reader never drains the pipe.
MAX_TAGGED_OUTPUT = 4096


def tagged_input(data: Json, tool_input: Json, original: str) -> str | None:
    """The hook output that tags ORIGINAL, or None when tagging is off."""
    if not tagging_enabled(data):
        return None
    text, sentences = tag_sentences(original)
    output = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {**tool_input, "message": text},
            }
        }
    )
    if len(output.encode()) + 1 > MAX_TAGGED_OUTPUT:
        message = "the tagged message is too large to write without blocking"
        raise ValueError(message)
    log_tagged(data, tool_input, sentences)
    return output


def abandon_stdout() -> None:
    """Drop the text a failed stdout still buffers.

    The interpreter flushes `sys.stdout` again at shutdown, and a second
    failure would change the exit status from 2 to 120.
    """
    sys.stdout = io.StringIO()


def send_message_reason(data: Json, tool_input: Json) -> str | None:
    """Tag a peer message when tagging is on, or return the refusal.

    Any failure on the tagging path keeps the refusal, so nothing there reaches
    main()'s fail-open exit.
    """
    original = str(tool_input.get("message", ""))
    bad = judged_claims(original)
    if not bad:
        return None
    try:
        output = tagged_input(data, tool_input, original)
        if output is not None:
            print(output)
            sys.stdout.flush()
            return None
    except Exception:  # noqa: BLE001  # any failure here must keep the refusal
        abandon_stdout()
    return claim_reason(bad)


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
    if tool == "SendMessage":
        return send_message_reason(data, tool_input)
    if tool in {"Write", "Edit"}:
        return open_note_reason(str(tool), tool_input)
    return None


def last_assistant_text(transcript: str) -> str:
    """The last assistant text since the last user prompt in a JSONL transcript."""
    text = ""
    try:
        with open(transcript, encoding="utf-8") as lines:  # noqa: PTH123  # pragma: no mutate -- encoding names ignore case
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                text = entry_text(entry, text)
    except OSError:
        return ""
    return text


def entry_text(entry: object, text: str) -> str:
    """TEXT after the transcript line ENTRY: reset by a prompt, set by a reply."""
    if not isinstance(entry, dict):
        return text
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if entry.get("type") == "user" and isinstance(content, str):
        return ""
    if entry.get("type") != "assistant" or not isinstance(content, list):
        return text
    blocks = cast("list[object]", content)  # pragma: no mutate -- a runtime no-op
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            fields = cast("Json", block)  # pragma: no mutate -- a runtime no-op
            text = str(fields.get("text", ""))
    return text


def stop_reason(data: Json) -> str | None:
    """Why the final reply DATA describes is blocked, or None."""
    if data.get("stop_hook_active"):
        return None
    message = data.get("last_assistant_message")
    if not message:
        message = last_assistant_text(str(data.get("transcript_path", "")))
    bad = judged_claims(str(message))
    return final_claim_reason(bad) if bad else None


def run(data: Json) -> int:
    """Handle one hook payload; the exit status."""
    event = data.get("hook_event_name")
    if event == "SessionStart":
        rule = userconfig.optional_string(TABLE, "rule")
        if rule:
            print(rule)
        return 0
    if event == "Stop":
        reason = stop_reason(data)
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}))
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
