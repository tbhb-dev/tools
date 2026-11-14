# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-gh-poll`: block GitHub polling and token unsets.

A PreToolUse hook beside ``guard-gh-pr``, with the same payload handling and
output contracts (``guard_gh_pr.command_of`` and ``decision``) and the same
shell-word scanner. GitHub CI and pull request state comes from awrel, which
relays webhook events into a session, so an agent never needs to poll GitHub.
These rules apply in every repository, and deny:

    gh run watch, gh run list, gh pr checks --watch
                                                 except the capped allowance
                                                 below
    gh api|query --paginate                      any endpoint
    gh api|query repos/O/R/actions/runs          and the other list endpoints:
                                                 actions/workflows/W/runs,
                                                 actions/jobs, check-runs,
                                                 check-suites
    while|until|for loops whose condition or body runs gh, or runs both
                                                 sleep and pgrep

The one exception is a granted allowance for a measurement pass: ``gh run list
-R OWNER/REPO --limit 100 --json FIELDS``, in exactly that order, for each
repository in a grant file under ``~/.local/state/tbhb``, one call for each
repository, and not after the grant's expiry date. A count file beside it
keeps the tally per repository. Only the operator writes either file: the guard
denies a Write or Edit to them and any shell command that names them. That
doesn't cover a script file or a computed path, so the allowance is a speed
bump against accidents, not a barrier.

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

import fcntl
import itertools
import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
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
# The capped allowance for one measurement pass. The operator writes the grant
# file, JSON with `expires` (an ISO date) and `repos` (OWNER/REPO names); without
# the file, or after `expires`, nothing is allowed. The names stay out of this
# repository. The grant allows one call per listed repository, and the count
# file records each repository once spent. Only the operator writes either file:
# the guard denies an agent's Write or Edit to them and a shell command that
# names them. It can't see a script file or a computed path, so this is a speed
# bump against accidents, not a barrier.
ALLOWANCE_GRANT = Path.home() / ".local/state/tbhb/gh-poll-allowance.json"
ALLOWANCE_COUNT = Path.home() / ".local/state/tbhb/gh-poll-allowance.count"
PROTECTED = (ALLOWANCE_GRANT.name, ALLOWANCE_COUNT.name)
PROTECT_REASON = (
    "The CI listing allowance's grant and count files are the operator's to "
    "write. Ask the coordinator."
)
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
DOTS = {".", ".."}
REPEATERS = {"xargs", "parallel"}
GH_RUN = re.compile(r"\bgh\b.*\brun\b", re.DOTALL)
REDIRECT = re.compile(r"\d*(?:>>?|<|>&|&>)")
REPEAT_REASON = (
    "Don't run a CI listing through xargs or parallel; each repeat is a separate "
    "call. Ask the coordinator."
)
ALLOWANCE_FIELDS = (
    "databaseId,event,conclusion,createdAt,startedAt,updatedAt,"
    "workflowName,attempt,headBranch"
)
ALLOWANCE_TAIL = ["--limit", "100", "--json", ALLOWANCE_FIELDS]
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


def granted_repos(grant: object) -> set[str]:
    """The lowercase repositories a grant lists; ValueError when it is malformed."""
    entries = grant.get("repos") if isinstance(grant, dict) else None
    repos: list[str] | None = entries if isinstance(entries, list) else None
    if repos is None or not all(
        isinstance(repo, str)
        and REPO.fullmatch(repo)
        and not DOTS & set(repo.split("/"))
        for repo in repos
    ):
        message = "malformed grant"
        raise ValueError(message)
    return {repo.lower() for repo in repos}


def allowed_call(args: list[str], repos: set[str]) -> bool:
    """Whether the words are exactly the allowed listing for a granted repository."""
    return (
        len(args) == len(ALLOWANCE_TAIL) + 2
        and args[0] == "-R"
        and args[1].lower() in repos
        and args[2:] == ALLOWANCE_TAIL
    )


def spend_allowance(args: list[str]) -> bool:
    """Whether ``gh run list``'s words after ``list`` use the granted allowance.

    The words must be exactly ``-R OWNER/REPO --limit 100 --json FIELDS`` for a
    repository the grant file lists, whatever the repository name's case. A
    match spends that repository's one call, recorded in the count file under
    a file lock. A missing, expired or malformed grant, a repository already
    spent, and an unreadable or unwritable count refuse it. The caller passes
    only a call with no global flag before ``run``.
    """
    try:
        grant = json.loads(ALLOWANCE_GRANT.read_text())
        if not allowed_call(args, granted_repos(grant)) or (
            datetime.now().astimezone().date() > date.fromisoformat(grant["expires"])
        ):
            return False
        ALLOWANCE_COUNT.parent.mkdir(parents=True, exist_ok=True)
        lock = ALLOWANCE_COUNT.with_name(ALLOWANCE_COUNT.name + ".lock")
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            spent = (
                json.loads(ALLOWANCE_COUNT.read_text())
                if ALLOWANCE_COUNT.exists()
                else {}
            )
            key = args[1].lower()
            if not isinstance(spent, dict) or key in {k.lower() for k in spent}:
                return False
            spent[key] = 1
            ALLOWANCE_COUNT.write_text(json.dumps(spent) + "\n")
    except OSError, ValueError, KeyError, TypeError, RecursionError:
        return False
    return True


def github_poll(args: list[str], *, repeated: bool = False) -> bool:
    """Whether the words after ``gh``, past its global flags, poll GitHub.

    A repeated listing never spends the allowance.
    """
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in VALUE_FLAGS else 1
    rest = args[i:]
    if rest[:2] == ["run", "list"]:
        return i > 0 or repeated or not spend_allowance(rest[2:])
    if rest[:2] == ["run", "watch"]:
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
    """The program's words after prefixes; None when they unset the token."""
    return unwrap(words)[0]


def unwrap(words: list[str]) -> tuple[list[str] | None, bool]:
    """The program's words after prefixes, and whether a repeater was passed.

    The words are None when the prefixes unset the token. A repeater is
    xargs or parallel as a prefix, or inside env's ``-S`` string.

    Prefixes are guard_gh_pr's: ``NAME=value`` words, shell keywords and its
    wrappers with their options. env's options are parsed as env does.
    ``TOKEN=`` empties the token.
    """
    rest: list[str] | None = words
    repeating = False
    while rest:
        word = rest[0]
        wrapper = program_of(rest)
        repeating = repeating or wrapper in REPEATERS
        if word == f"{TOKEN}=":
            return None, repeating
        if REDIRECT.match(word):
            rest = rest[1 if REDIRECT.fullmatch(word) is None else 2 :]
        elif word in KEYWORDS or ASSIGNMENT.match(word):
            rest = rest[1:]
        elif wrapper == "env":
            rest = env_rest(rest[1:])
        elif wrapper in WRAPPERS:
            rest = rest[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
        else:
            break
    return rest, repeating


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


def repeats(command: list[str]) -> bool:
    """Whether xargs or parallel would run a ``gh run`` command more than once.

    The listing words can arrive from stdin or ``-a``, so ``gh run`` alone counts.
    """
    names = {word.rpartition("/")[2] for word in command}
    return bool(REPEATERS & names) and bool(GH_RUN.search(" ".join(command)))


def repeated_scan(args: list[str]) -> tuple[str | None, set[str]]:
    """Scan what a repeater may run: each suffix of its words, as repeated shell text.

    The repeater's own options and the program they lead to aren't told
    apart, so every suffix is read as a command; a suffix that isn't one
    finds nothing.
    """
    programs: set[str] = set()
    for i in range(len(args)):
        message, seen = inspect(shlex.join(args[i:]), repeated=True)
        if message:
            return message, programs
        programs |= seen
    return None, programs


def denial(
    command: list[str], body: str | None, *, repeated: bool = False
) -> tuple[str | None, set[str]]:
    """One simple command's deny reason, and the programs it runs.

    ``repeated`` is set when xargs or parallel runs the command more than once,
    so a ``gh run list`` can't spend the one-call allowance.
    """
    words, passed = unwrap(command)
    repeated = repeated or passed
    if words is None:
        return UNSET_REASON, set()
    if not words:
        return None, set()
    program = program_of(words)
    unset = program == "unset" and any(names_token(word) for word in words[1:])
    if unset or clears_token(program, words[1:]):
        return UNSET_REASON, set()
    if program == "gh" and github_poll(words[1:], repeated=repeated):
        return POLL_REASON, set()
    script = inner_script(words, body)
    if program in REPEATERS:
        message, inner = repeated_scan(words[1:])
    elif script is None:
        return None, {program}
    else:
        message, inner = inspect(script, repeated=repeated)
    return message, {program, *inner}


def inspect(text: str, *, repeated: bool = False) -> tuple[str | None, set[str]]:
    """The deny reason for a shell command, or None, and the programs it runs."""
    if any(name in text.lower() for name in PROTECTED):
        return PROTECT_REASON, set()
    return scan_commands(text, repeated=repeated)


def checked(
    command: list[str], body: str | None, *, repeated: bool = False
) -> tuple[str | None, set[str]]:
    """One simple command's deny reason, refusing a repeated listing first."""
    if repeats(command):
        return REPEAT_REASON, set()
    return denial(command, body, repeated=repeated)


def scan_commands(text: str, *, repeated: bool = False) -> tuple[str | None, set[str]]:
    """Scan each command of a shell text for a denial.

    Each open loop collects the programs run from its keyword to its
    ``done``; an outer loop also collects its inner loops' programs.
    """
    loops: list[set[str]] = []
    programs: set[str] = set()
    scanner = BodyScanner(text)
    for index, command in enumerate(scanner.run()):
        if opens_loop(command):
            loops.append(set())
        message, seen = checked(command, scanner.bodies.get(index), repeated=repeated)
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


def edited_file(data: dict[str, object]) -> str:
    """The path a Write, Edit or NotebookEdit call targets, or "" for another call."""
    inputs = data.get("tool_input")
    if not isinstance(inputs, dict):
        return ""
    path = inputs.get("file_path") or inputs.get("notebook_path")
    return path if isinstance(path, str) else ""


def protects(path: str) -> bool:
    """Whether an edit target is a protected file, by name in any case or by link.

    A name that starts with a protected name also counts, such as a lock or backup copy.
    """
    if not path:
        return False
    names = {Path(path).name.lower(), Path(path).resolve().name.lower()}
    if any(name.startswith(PROTECTED) for name in names):
        return True
    for target in (ALLOWANCE_GRANT, ALLOWANCE_COUNT):
        try:
            if Path(path).samefile(target):
                return True
        except OSError:  # the target or the protected file doesn't exist
            continue
    return False


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        data = read_hook_stdin()
        text, antigravity = command_of(data)
        message = inspect(text)[0]
        if protects(edited_file(data)):
            message = PROTECT_REASON
        if message:
            print(json.dumps(decision(message, antigravity=antigravity)))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
