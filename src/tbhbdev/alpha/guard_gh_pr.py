# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-gh-pr`: block direct `gh pr` writes and self-approval.

A PreToolUse hook, ported from ascm's interim tools. Outside a tbhb-dev
repository only the approval rules below apply. Inside one, agents open, edit
and merge pull requests through `tbhbdev alpha pr` and
`tbhbdev alpha merge`, which add the machine-written trailers and
the plain-text squash message. This hook blocks the direct `gh` call and names
the command to run instead:

    gh pr create  ->  tbhbdev alpha pr check, then tbhbdev alpha pr create
    gh pr edit    ->  tbhbdev alpha pr edit --pr N
    gh pr merge   ->  tbhbdev alpha merge N   (--dry-run previews)

It reads the hook payload on stdin and takes no arguments. The payload shape
alone picks the output contract, so the hook behaves the same whoever runs it:

- Antigravity: ``toolCall.args.CommandLine``; denies with
  ``{"decision": "deny", "reason": ...}``.
- Claude Code and Codex: ``tool_input.command``; deny with
  ``hookSpecificOutput.permissionDecision`` and
  ``permissionDecisionReason``, which both harnesses show to the model.

A session can't approve a pull request it or its subagents opened. The guard
reads the caller's root session from the payload (see ``tbhbdev.alpha.opened``)
and denies these approval routes when that root's opened ledger lists the
target, in any repository, tbhb-dev or not:

    gh pr review N --approve|-a      repo from -R/--repo or a PR URL, else origin
    gh api repos/O/R/pulls/N/reviews  a POST (-X/--method POST, or any
                                      -f/-F/--field/--raw-field) whose fields
                                      hold event=APPROVE; also .../reviews/ID/events
    tbhb-approve REPO N SHA           read as tbhb-dev/REPO#N

Without ``-R``, the repository is a ``GH_REPO=`` prefix on the command, else
the hook cwd's origin, which also fills ``{owner}`` and ``{repo}`` in a
``gh api`` path, as gh does. After a ``cd``, ``pushd``, ``popd`` or
``git -C`` earlier in the command line the cwd is unknown, and so is a target
that would come from it. PR numbers lose leading zeros. These routes fail
closed: a ledger that exists but can't be read, a payload whose root session
can't be resolved, a crash while checking, and,
for a root whose ledger lists any pull request, an approval whose target
can't be read (``gh pr review --approve`` on the current branch, say) all
deny. A root with no ledger opened nothing, so its approvals pass.

Allowing prints nothing. Every other path fails open: unreadable input or any crash
allows the command and prints nothing, because a broken hook must never stop
the agent's work. The permission rules in .claude/settings.json and
.codex/rules/pull-request.rules stay as backstops.

Matching is by shell word, not by regex over the text. A small scanner splits
the command into simple commands at ``;``, ``&``, ``|``, newlines, parentheses,
``$(...)`` and backticks (inside double quotes too), drops heredoc bodies, and
removes quoting. A simple command matches when, after leading ``NAME=value``
words, shell keywords and the wrappers env, command, exec, sudo, nohup, time,
builtin and xargs (with their options), the program is ``gh`` (any path) and,
after its global flags (``-R x``, ``--repo x``, ``--hostname x``, other
``-x``), the words are ``pr create|edit|merge``. ``bash|sh|zsh|dash|ksh -c
SCRIPT`` and ``eval ...`` are scanned recursively. Text inside a quoted
argument to any other command is not a command, so
``git commit -m 'docs: mention gh pr merge'`` and ``echo "gh pr merge"`` pass.

Known gaps, none of which the permission rules close either:

- Misses: a wrapper outside the list (``timeout 5 gh ...``, ``mise x -- gh
  ...``, ``sudo -u root gh ...``), a script file or alias that runs gh, code a
  command runs from a quoted argument (``python -c``, ``ssh host '...'``), and
  ``gh api`` calls that do the same work. Substitutions inside an unquoted
  heredoc body are not scanned.
- Approvals it misses: ``gh api graphql`` with an ``addPullRequestReview`` or
  ``submitPullRequestReview`` mutation, ``gh api --input FILE`` and
  ``-F event=@file``, curl, httpx or any client other than gh, a script or
  wrapper outside the list that approves, another host, and a session
  without the user-level hooks. It reads only this host's ledger, so a pull
  request opened elsewhere, or before the rollout and not yet backfilled, is
  not covered, and an edit to the ledger hides an opener. None of this is a
  security boundary.
- False positives: only text the shell itself runs is read as a command, so
  quoted words never match. The remaining case is a deliberate one: a
  ``$(gh pr merge 1)`` inside ``echo`` or a commit message is run by the shell
  and is blocked.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

from tbhbdev.alpha.opened import LedgerError, caller_root, read_ledger
from tbhbdev.alpha.repo import git, hook_cwd, in_tbhb_dev, read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

type Json = dict[str, object]

SUBCOMMANDS = ("create", "edit", "merge")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
WRAPPERS = {"env", "command", "exec", "sudo", "nohup", "time", "builtin", "xargs"}
KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{", "}"}
VALUE_FLAGS = {"-R", "--repo", "--hostname"}
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
PR_NUMBER = re.compile(r"(?:\S*/pull/|#)?(\d+)")
SEPARATORS = ";&|"

DRAFT = ".github/PULL_REQUEST_DRAFT.md"
TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"

RULE = "A session can't approve a pull request it or its subagents opened"
APPROVE_SCRIPT = "tbhb-approve"
APPROVE_OWNER = "tbhb-dev"
# gh pr review's flags that take a value; the next word is not the PR.
REVIEW_VALUE_FLAGS = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}
# gh api's flags that take a value; the next word is not the endpoint.
API_VALUE_FLAGS = {
    "-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header",
    "--input", "-q", "--jq", "-t", "--template", "--cache", "-p", "--preview",
    "--hostname",
}  # fmt: skip
API_FIELD_FLAGS = {"-f", "--raw-field", "-F", "--field"}
APPROVE_FIELD = re.compile(r"\s*event\s*=\s*(?i:approve)\s*")
PR_URL = re.compile(r"https?://[^/\s]+/([^/\s]+/[^/\s]+)/pull/(\d+)\S*")
REVIEWS = re.compile(
    r"(?:https?://[^/]+/)?/?repos/([^/]+)/([^/]+)/pulls/([^/]+)/reviews"
    r"(?:/[^/?#]+/events)?/?(?:\?.*)?"
)


class Match(NamedTuple):
    """A blocked `gh pr` call: the subcommand and the words after it."""

    sub: str
    args: list[str]


@dataclass
class Scanner:
    """Split shell text into simple commands, each a list of unquoted words."""

    text: str
    pos: int = 0
    quote: str = ""
    word: list[str] = field(default_factory=list)
    in_word: bool = False
    words: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    # One (kind, quote to restore) per open `$(`, `(` or backtick.
    nesting: list[tuple[str, str]] = field(default_factory=list)
    heredocs: list[tuple[str, bool]] = field(default_factory=list)

    def end_word(self) -> None:
        """Close the word being read, if any."""
        if self.in_word:
            self.words.append("".join(self.word))
        self.word = []
        self.in_word = False

    def end_command(self) -> None:
        """Close the word and the simple command being read."""
        self.end_word()
        if self.words:
            self.commands.append(self.words)
        self.words = []

    def add(self, char: str) -> None:
        """Append a literal character to the current word."""
        self.word.append(char)
        self.in_word = True

    def open_nesting(self, kind: str, width: int) -> None:
        """Enter ``$(``, ``(`` or a backtick: a new command, unquoted."""
        self.end_command()
        self.nesting.append((kind, self.quote))
        self.quote = ""
        self.pos += width

    def close_nesting(self) -> None:
        """Leave a ``)`` or a closing backtick, restoring the outer quote."""
        self.end_command()
        if self.nesting:
            self.quote = self.nesting.pop()[1]
        self.pos += 1

    def backtick(self) -> None:
        """A backtick opens a substitution, or closes the one that is open."""
        if self.nesting and self.nesting[-1][0] == "`":
            self.close_nesting()
        else:
            self.open_nesting("`", 1)

    def heredoc(self) -> None:
        """Read the delimiter after ``<<`` and queue the body to be skipped."""
        self.pos += 2
        strip = self.text.startswith("-", self.pos)
        self.pos += strip
        while self.text[self.pos : self.pos + 1] in {" ", "\t"}:
            self.pos += 1
        delim: list[str] = []
        quote = ""
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if quote:
                if char == quote:
                    quote = ""
                else:
                    delim.append(char)
            elif char in "'\"":
                quote = char
            elif char.isspace() or char in SEPARATORS + "()<>":
                break
            else:
                delim.append(char)
            self.pos += 1
        self.heredocs.append(("".join(delim), strip))

    def skip_heredoc_bodies(self) -> None:
        """After a newline, drop each queued heredoc body up to its delimiter."""
        for delim, strip in self.heredocs:
            while self.pos < len(self.text):
                end = self.text.find("\n", self.pos)
                end = len(self.text) if end < 0 else end
                line = self.text[self.pos : end]
                self.pos = min(end + 1, len(self.text))
                if (line.lstrip("\t") if strip else line) == delim:
                    break
        self.heredocs = []

    def quoted(self, char: str) -> None:
        """One character inside single or double quotes."""
        if char == self.quote:
            self.quote = ""
        elif self.quote == '"' and char == "\\":
            self.add(self.text[self.pos + 1 : self.pos + 2])
            self.pos += 1
        elif self.quote == '"' and self.text.startswith("$(", self.pos):
            self.open_nesting("$", 2)
            return
        elif self.quote == '"' and char == "`":
            self.backtick()
            return
        else:
            self.add(char)
        self.pos += 1

    def unquoted(self, char: str) -> None:
        """One character outside quotes."""
        if self.text.startswith("$(", self.pos):
            self.open_nesting("$", 2)
        elif self.text.startswith("<<<", self.pos):
            self.end_word()
            self.pos += 3
        elif self.text.startswith("<<", self.pos):
            self.end_word()
            self.heredoc()
        elif char == "`":
            self.backtick()
        elif char == "(":
            self.open_nesting("(", 1)
        elif char == ")":
            self.close_nesting()
        else:
            self.plain(char)

    def plain(self, char: str) -> None:
        """Quotes, escapes, separators, comments and ordinary characters."""
        self.pos += 1
        if char == "\\":
            if self.text[self.pos : self.pos + 1] != "\n":
                self.add(self.text[self.pos : self.pos + 1])
            self.pos += 1
        elif char in "'\"":
            self.quote, self.in_word = char, True
        elif char == "\n":
            self.end_command()
            self.skip_heredoc_bodies()
        elif char in SEPARATORS:
            self.end_command()
        elif char.isspace():
            self.end_word()
        elif char == "#" and not self.in_word:
            end = self.text.find("\n", self.pos)
            self.pos = len(self.text) if end < 0 else end
        else:
            self.add(char)

    def run(self) -> list[list[str]]:
        """Scan the whole text."""
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if self.quote:
                self.quoted(char)
            else:
                self.unquoted(char)
        self.end_command()
        return self.commands


def skip_prefix(words: list[str]) -> list[str]:
    """The command's words after env assignments, keywords and wrappers."""
    i = 0
    while i < len(words):
        word = words[i]
        if word in KEYWORDS or ASSIGNMENT.match(word):
            i += 1
        elif word.rsplit("/", 1)[-1] in WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        else:
            break
    return words[i:]


def gh_match(rest: list[str]) -> Match | None:
    """Match the words after ``gh``: global flags, then ``pr create|edit|merge``."""
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in VALUE_FLAGS else 1
    if rest[i : i + 1] == ["pr"] and rest[i + 1 : i + 2] and rest[i + 1] in SUBCOMMANDS:
        return Match(rest[i + 1], rest[i + 2 :])
    return None


def shell_script(rest: list[str]) -> str | None:
    """The script word of ``-c`` among a shell's options, if it has one."""
    for i, word in enumerate(rest):
        if not word.startswith("-"):
            return None
        if not word.startswith("--") and "c" in word:
            return rest[i + 1] if i + 1 < len(rest) else None
    return None


def program_of(words: list[str]) -> str:
    """A command's program name, without its directory."""
    return words[0].rpartition("/")[2]


class Simple(NamedTuple):
    """A simple command: assignments and wrappers, then the program's words."""

    prefix: list[str]
    words: list[str]


def simple_commands(text: str) -> Iterator[Simple]:
    """Each simple command the shell would run, split from its prefix words.

    ``bash -c SCRIPT`` and ``eval`` are scanned in their place, under the
    shell's own prefix.
    """
    for command in Scanner(text).run():
        words = skip_prefix(command)
        if not words:
            continue
        prefix = command[: len(command) - len(words)]
        program = program_of(words)
        if program in SHELLS:
            script = shell_script(words[1:])
            inner = simple_commands(script) if script is not None else iter(())
        elif program == "eval":
            inner = simple_commands(" ".join(words[1:]))
        else:
            yield Simple(prefix, words)
            continue
        for found in inner:
            yield Simple(prefix + found.prefix, found.words)


def find(text: str) -> Match | None:
    """The first direct `gh pr create|edit|merge` call in a shell command."""
    for _, words in simple_commands(text):
        found = gh_match(words[1:]) if program_of(words) == "gh" else None
        if found:
            return found
    return None


class Approval(NamedTuple):
    """An approval route: what ran, and its ``owner/repo#N`` when readable."""

    route: str
    target: str | None


def repo_name(value: str) -> str | None:
    """``owner/repo`` from ``[HOST/]OWNER/REPO``; None without both parts."""
    parts = value.rstrip("/").removesuffix(".git").split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 and all(parts[-2:]) else None


def origin_repo(cwd: Path | None) -> str | None:
    """``owner/repo`` of the origin remote of ``cwd``, or None."""
    code, url = git("remote", "get-url", "origin", cwd=cwd)
    return repo_name(url.strip().replace(":", "/")) if code == 0 else None


def flag_word(word: str, value_flags: set[str]) -> tuple[str, str | None] | None:
    """The value flag a flag word sets, with its attached value, or None.

    ``--flag=v`` and ``-Xv`` attach the value. ``--flag`` and ``-X``, also at
    the end of a cluster such as ``-aX``, take the next word (None here).
    """
    if word.startswith("--"):
        name, equals, value = word.partition("=")
        return (name, value if equals else None) if name in value_flags else None
    for index, char in enumerate(word[1:], 2):
        if f"-{char}" in value_flags:
            return f"-{char}", word[index:] or None
    return None


def flag_values(words: list[str], flags: set[str], value_flags: set[str]) -> list[str]:
    """The values of ``flags``, read from flag words only, not other values."""
    values: list[str] = []
    rest = iter(words)
    for word in rest:
        found = flag_word(word, value_flags) if word.startswith("-") else None
        if found is None:
            continue
        name, value = found
        value = next(rest, None) if value is None else value
        if name in flags and value is not None:
            values.append(value)
    return values


def takes_value(word: str, value_flags: set[str]) -> bool:
    """Whether a flag word's value is the next word: ``-b``, or ``-ab`` ending in it."""
    found = flag_word(word, value_flags)
    return found is not None and found[1] is None


def split_args(words: list[str], value_flags: set[str]) -> tuple[list[str], list[str]]:
    """(flag words, positional words); a flag's separate value is in neither."""
    flags: list[str] = []
    found: list[str] = []
    rest = iter(words)
    for word in rest:
        if word.startswith("-"):
            flags.append(word)
            if takes_value(word, value_flags):
                next(rest, None)
        else:
            found.append(word)
    return flags, found


def approves(word: str) -> bool:
    """Whether a gh pr review flag word turns on ``--approve``."""
    if word == "--approve" or word.startswith("--approve="):
        return True
    if word.startswith("--"):
        return False
    for char in word[1:]:
        if char == "a":
            return True
        if f"-{char}" in REVIEW_VALUE_FLAGS:
            return False
    return False


def review_approval(
    args: list[str], repos: list[str], default: Callable[[], str | None]
) -> Approval:
    """The target of ``gh pr review --approve``, from its positional words."""
    route = "`gh pr review --approve`"
    selector = next(iter(args), "")  # pragma: no mutate -- any non-PR default
    if url := PR_URL.fullmatch(selector):
        return Approval(route, f"{url[1]}#{int(url[2])}")
    number = PR_NUMBER.fullmatch(selector)
    repo = repo_name(repos[-1]) if repos else default()
    return Approval(route, f"{repo}#{int(number[1])}" if number and repo else None)


def posts_approve(args: list[str]) -> bool:
    """Whether ``gh api`` args send a POST whose fields hold ``event=APPROVE``."""
    fields = flag_values(args, API_FIELD_FLAGS, API_VALUE_FLAGS)
    methods = flag_values(args, {"-X", "--method"}, API_VALUE_FLAGS)
    if methods and methods[-1].upper() != "POST":
        return False
    return any(APPROVE_FIELD.fullmatch(field) for field in fields)


def api_approval(args: list[str], default: Callable[[], str | None]) -> Approval | None:
    """A ``gh api`` POST of an APPROVE review, or None."""
    endpoints = split_args(args, API_VALUE_FLAGS)[1]
    endpoint = REVIEWS.fullmatch(endpoints[0]) if endpoints else None
    if endpoint is None or not posts_approve(args):
        return None
    owner, repo, number = endpoint[1], endpoint[2], endpoint[3]
    if owner == "{owner}" or repo == "{repo}":
        here = (default() or "/").split("/")
        owner = here[0] if owner == "{owner}" else owner
        repo = here[1] if repo == "{repo}" else repo
    name = repo_name(f"{owner}/{repo}")
    target = f"{name}#{int(number)}" if name and number.isdecimal() else None
    return Approval("a `gh api` APPROVE review", target)


def gh_approval(rest: list[str], default: Callable[[], str | None]) -> Approval | None:
    """An approval through gh: ``pr review --approve`` or an api review POST.

    ``default`` gives the repository gh uses without ``-R``.
    """
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in VALUE_FLAGS else 1
    if rest[i : i + 2] == ["pr", "review"]:
        flags, args = split_args(rest[i + 2 :], REVIEW_VALUE_FLAGS)
        if any(approves(word) for word in flags):
            value_flags = VALUE_FLAGS | REVIEW_VALUE_FLAGS
            repos = flag_values(rest, {"-R", "--repo"}, value_flags)
            return review_approval(args, repos, default)
    if rest[i : i + 1] == ["api"]:
        return api_approval(rest[i + 1 :], default)
    return None


def script_approval(args: list[str]) -> Approval | None:
    """``tbhb-approve REPO N SHA``, read as ``tbhb-dev/REPO#N``."""
    if len(args) < 2 or not args[1].isdecimal():  # noqa: PLR2004 -- REPO and N
        return None
    return Approval(f"`{APPROVE_SCRIPT}`", f"{APPROVE_OWNER}/{args[0]}#{int(args[1])}")


def default_repo(
    prefix: list[str], cwd: Path | None, *, moved: bool
) -> Callable[[], str | None]:
    """The repository gh uses without ``-R``: ``GH_REPO=``, else cwd's origin.

    After a directory change the cwd is unknown, so the origin is too.
    """
    names = [
        word.removeprefix("GH_REPO=") for word in prefix if word.startswith("GH_REPO=")
    ]

    def repo() -> str | None:
        if names:
            return repo_name(names[-1])
        return None if moved else origin_repo(cwd)

    return repo


def changes_directory(words: list[str]) -> bool:
    """Whether a command moves the shell, or git, to another directory."""
    program = program_of(words)
    return program in {"cd", "pushd", "popd"} or (program == "git" and "-C" in words)


def approvals(text: str, cwd: Path | None) -> list[Approval]:
    """Every approval route in a shell command."""
    found: list[Approval] = []
    moved = False  # pragma: no mutate -- None is as falsy as False here
    for prefix, words in simple_commands(text):
        program = program_of(words)
        if program == "gh":
            match = gh_approval(words[1:], default_repo(prefix, cwd, moved=moved))
        elif program == APPROVE_SCRIPT:
            match = script_approval(words[1:])
        else:
            moved = moved or changes_directory(words)
            continue
        if match:
            found.append(match)
    return found


def denial(found: list[Approval], data: Json, env: Mapping[str, str]) -> str | None:
    """The deny message for approval routes, or None when the caller may approve.

    Fails closed: an unknown root or an unreadable ledger denies.
    """
    root = caller_root(data, env)
    if root is None:
        return (
            f"{RULE}, and the guard can't tell which session this is, "
            f"so it refuses {found[0].route}."
        )
    try:
        opened = {ref.lower() for ref in read_ledger(root)}
    except LedgerError as exc:
        return (
            f"{RULE}, and the guard can't read this session's ledger, "
            f"so it refuses {found[0].route}: {exc}"
        )
    for approval in found:
        if approval.target is None and opened:
            return (
                f"{RULE}, and the guard can't tell which pull request {approval.route} "
                "approves. Name it as OWNER/REPO and number."
            )
        if approval.target and approval.target.lower() in opened:
            return (
                f"{RULE}: this session opened {approval.target}, "
                "so it can't approve it."
            )
    return None


def hint(found: Match) -> str:
    """The block message: what was blocked and the command to run instead."""
    blocked = f"`gh pr {found.sub}` is blocked."
    number = next((m[1] for w in found.args if (m := PR_NUMBER.fullmatch(w))), "<N>")
    if found.sub == "create":
        return (
            f"{blocked} Write {DRAFT} from {TEMPLATE}, check it with "
            "`tbhbdev alpha pr check`, then open the PR with `tbhbdev alpha pr create`."
        )
    if found.sub == "edit":
        return (
            f"{blocked} Update {DRAFT}, then run `tbhbdev alpha pr edit --pr {number}`."
        )
    return (
        f"{blocked} Once the PR is approved, run `tbhbdev alpha merge {number}` "
        "(add `--dry-run` to preview the squash message first)."
    )


def command_of(data: Json) -> tuple[str, bool]:
    """(shell command, whether the payload is Antigravity's) from hook stdin."""
    call = data.get("toolCall")
    if isinstance(call, dict):
        args = call.get("args")
        text = args.get("CommandLine") if isinstance(args, dict) else None
        return (text if isinstance(text, str) else ""), True
    inputs = data.get("tool_input")
    text = inputs.get("command") if isinstance(inputs, dict) else None
    return (text if isinstance(text, str) else ""), False


def decision(message: str, *, antigravity: bool) -> Json:
    """The deny output in the harness's own contract."""
    if antigravity:
        return {"decision": "deny", "reason": message}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


def approval_message(data: Json, text: str) -> str | None:
    """The deny message for the approval routes in ``text``, or None.

    Matching fails open with the rest of the guard; once a route matches, a
    crash denies.
    """
    try:
        found = approvals(text, hook_cwd(data))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return None
    if not found:
        return None
    try:
        return denial(found, data, os.environ)
    except Exception as exc:  # noqa: BLE001  # fail closed for an approval
        return (
            f"{RULE}, and the guard failed while checking, "
            f"so it refuses {found[0].route}: {exc}"
        )


def main() -> int:
    """Read the payload on stdin; print a deny decision for a blocked call."""
    try:
        data = read_hook_stdin()
        text, antigravity = command_of(data)
        message = approval_message(data, text)
        if message:
            print(json.dumps(decision(message, antigravity=antigravity)))
            return 0
        if not in_tbhb_dev(hook_cwd(data)):
            return 0
        found = find(text)
        if found:
            print(json.dumps(decision(hint(found), antigravity=antigravity)))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
