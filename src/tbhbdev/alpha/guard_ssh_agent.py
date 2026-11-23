# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-ssh-agent`: keep agent commands off the SSH agent.

This PreToolUse hook shares guard-gh-pr's shell scanner and payload contracts.
It inspects the command string without running it and denies:

- ssh, scp, sftp, and rsync over ssh, unless the call passes
  `-o IdentityAgent=none`, or passes `-i KEY` together with
  `-o IdentitiesOnly=yes`;
- `ssh-add`;
- any word that names a path under the operator's ssh directory in their home,
  except its `sessions` subdirectory;
- a command that sets SSH_AUTH_SOCK to a nonempty value.

Unsetting SSH_AUTH_SOCK is allowed. Like the other guards, allowing prints
nothing and unreadable input or a crash fails open.

Known gaps: a program name held in a variable, `timeout N ssh` and other
runners, `GIT_SSH_COMMAND` and `core.sshCommand` settings, and ssh config
files that set options.
"""

import json
import re
from pathlib import Path

from tbhbdev.alpha.guard_gh_pr import (
    command_of,
    decision,
    program_of,
    simple_commands,
)
from tbhbdev.alpha.repo import read_hook_stdin

SSH_PROGRAMS = {"ssh", "scp", "sftp"}
REASON_AGENT = (
    "The ssh call is blocked: pass -o IdentityAgent=none, or -i KEY with "
    "-o IdentitiesOnly=yes, so it never reaches the SSH agent."
)
REASON_ADD = "ssh-add is blocked: agent sessions never load keys into an agent."
REASON_DIR = (
    "The command is blocked: it names a path in the operator's ssh directory "
    "outside its sessions subdirectory."
)
REASON_SOCK = "The command is blocked: it sets SSH_AUTH_SOCK."
NO_AGENT = re.compile(r"IdentityAgent[=\s]+none\b", re.IGNORECASE)
ONLY_KEYS = re.compile(r"IdentitiesOnly[=\s]+yes\b", re.IGNORECASE)
SSH_DIR = ".ssh"
SOCK = re.compile(r"SSH_AUTH_SOCK=(?=\S)")


def avoids_agent(words: list[str]) -> bool:
    """Whether ssh-style words refuse the agent or pin explicit keys."""
    text = " ".join(words)
    if NO_AGENT.search(text):
        return True
    has_key = any(w.startswith("-i") for w in words)
    return has_key and bool(ONLY_KEYS.search(text))


def rsync_uses_ssh(words: list[str]) -> bool:
    """Whether an rsync call has a remote operand or an -e/--rsh command."""
    rest = words[1:]
    remote = any(":" in w and not w.startswith(("-", "/", ".")) for w in rest)
    return remote or any(w == "-e" or w.startswith(("--rsh", "-e")) for w in rest)


def names_ssh_dir(word: str) -> bool:
    """Whether a word names the ssh directory or a path below it, not sessions."""
    home = str(Path.home())
    for prefix in ("~", "$HOME", "${HOME}", home):
        for part in re.split(r"[=:]", word):
            base = f"{prefix}/{SSH_DIR}"
            if part == base or part.startswith(base + "/"):
                sessions = f"{base}/sessions"
                if part != sessions and not part.startswith(sessions + "/"):
                    return True
    return False


def verdict(text: str) -> str | None:
    """The deny reason for a command, or None to allow it."""
    for prefix, words in simple_commands(text):
        if any(SOCK.match(w) for w in [*prefix, *words]):
            return REASON_SOCK
        if any(names_ssh_dir(w) for w in [*prefix, *words]):
            return REASON_DIR
        program = program_of(words)
        if program == "ssh-add":
            return REASON_ADD
        if program in SSH_PROGRAMS and not avoids_agent(words):
            return REASON_AGENT
        if program == "rsync" and rsync_uses_ssh(words) and not avoids_agent(words):
            return REASON_AGENT
    return None


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        text, antigravity = command_of(read_hook_stdin())
        reason = verdict(text)
        if reason:
            print(json.dumps(decision(reason, antigravity=antigravity)))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
