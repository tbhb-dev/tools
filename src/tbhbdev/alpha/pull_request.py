# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha pr`: pull-request tooling, ported from an earlier interim toolset.

Agents draft a pull request as the gitignored .github/PULL_REQUEST_DRAFT.md.
Its YAML frontmatter is PR metadata (.github/schemas/pull-request-draft.schema.json),
its H1 is the PR title, and the rest is the description, shaped by
.github/PULL_REQUEST_TEMPLATE.md. This script checks the draft and opens the PR.

Usage, from anywhere in a worktree:

    tbhbdev alpha pr check [DRAFT]
    tbhbdev alpha pr create [DRAFT] [--dry-run]
    tbhbdev alpha pr edit DRAFT --pr N
    tbhbdev alpha pr ready N [N...]
    tbhbdev alpha pr rebase N [N...]
    tbhbdev alpha pr rerun N [--stuck]
    tbhbdev alpha pr delete-branch N
    tbhbdev alpha pr checks N
    tbhbdev alpha pr hook claude|codex|antigravity
    tbhbdev alpha pr backfill-opened OWNER

The repository is the worktree the command runs in: its schema, section
scheme and lint configs are the ones checked against. For ``check`` and
``create``, DRAFT defaults to that worktree's .github/PULL_REQUEST_DRAFT.md.
``edit`` requires an explicit DRAFT path. Every failure prints as
`path:line: problem`, and the exit code is 1 when there is any.

``check`` validates the frontmatter against the schema, requires a title that
``cog verify`` accepts and that fits 64 characters (a merge appends
`` (#N)`` and the commit header must fit 72), checks the H2 sections against
.github/pull-request-sections.toml (names, order, required, word limits), rejects
leftover HTML comments and bare ``#N`` references, and lints the description
with vale, cspell and rumdl under the repository's own configs. The vale
style packages under .vale/ are gitignored, so a fresh worktree lacks them:
when a style that .vale.ini's ``BasedOnStyles`` names is missing, ``check``
checks for ``//:vale_sync`` before running the repository's pinned
``bazelisk run //:vale_sync`` once, and reports one problem instead of linting
with vale if the target or styles are still missing.

``hook`` is the harnesses' PostToolUse entry point (payload on stdin). Outside
a tbhb-dev repository it exits 0 at once. When the tool call wrote the draft of
the worktree the payload names it runs ``check``'s checks and, on problems, exits 2
with them on stderr so the agent fixes the draft. Other calls exit 0 at once.

``create`` and ``edit`` run ``check``, then append the ``Generated-by`` and
``Assisted-by`` trailers as the body's last paragraph, so a merge carries
them into the commit. ``create`` reads the local branch's commits. ``edit``
reads the pull request's commits on GitHub and refuses the edit if it cannot
list them. The trailers are deduplicated by identity, plus the posting agent
as ``Assisted-by``. They are machine-written: a draft that contains one fails
``check``, because a person cannot compute the session hashes by hand and a
stripped line would hide what the author thought they had written. Before gh
posts, and in a dry run too, the final title and body, trailers included, go
through gitleaks under the host's private config (see
``tbhbdev.alpha.secret_scan``), and a finding, a missing config or a missing
gitleaks stops the command. Nothing is pushed: when the branch is not on the
remote, gh's own error is passed through.

In a private repository, read live with ``gh repo view`` once per command, that
scan skips the private config's name rules (the ``NAME_RULES`` families). This
matches the rules' gates, which apply the private config only to a repository
that sets the flag, so text that names the repository's own subject passes.
The visibility is that of the repository the text is posted to, not the
working directory's. ``edit`` takes that repository from the pull request's
own ``url`` and reads its visibility with ``gh repo view <owner/name>``.
``create`` has no pull request yet, so it reads ``gh repo view`` for the base
repository, which is gh's own resolution and the one ``gh pr create`` uses. A
create target that can't be resolved scans under the full private config.
The ``op-*`` rules, the address, MAC, endpoint and keychain rules, the canary
and .gitleaks.toml still apply. Any visibility but ``PRIVATE`` or a failed
read scans under the full private config, so the read alone never refuses a
command.
``check`` makes no gh calls and doesn't scan.

``create`` also writes ``Opened-by: LLM ...`` as the first line of that
paragraph: the posting agent's root identity, with no ``subagent=``. Once gh
has opened the pull request, it adds ``owner/repo#N`` to that root's opened
ledger (see ``tbhbdev.alpha.opened``), which the guard reads to refuse the
session's own approval. An agent whose root can't be resolved, or a human,
gets neither, and so does a root whose value fails the trailer grammar (no
model, say), with a warning. A ledger that can't be written after the pull
request exists only warns. ``edit`` copies the live body's ``Opened-by`` line unchanged,
and adds none when the live body has none. ``check`` rejects a draft that
contains one.

``ready N [N...]`` marks each pull request ready for review with ``gh pr ready``,
in order, but only when its GitHub author is the account this ``gh`` runs as. It
stops at the first failure and doesn't read a draft.

``rerun N`` reruns only the failed jobs of the latest run for pull request N's
current head commit with ``gh run rerun --failed``. It reports an error when
that run hasn't failed or no run exists. It doesn't read a draft.

``rerun N --stuck`` cancels a ``pull_request`` run of pull request N's current
head when one of its jobs has been in progress for over an hour, then reruns
it in full once the run completes as cancelled. It reports an error when no
such run is in progress or no job is that old.

``delete-branch N`` deletes only the head branch of a closed or merged pull
request in origin's repository. Fork heads, main, protected branches and
unreadable metadata are refused. It doesn't read a draft.

``checks N`` prints pull request N's head SHA, then one line per check
with its name and conclusion. A check run that hasn't COMPLETED reads its
status, never its conclusion; unfinished checks
print ``PENDING``. A commit status reads only its state, and only ``SUCCESS``
is green. Any
conclusion other than ``SUCCESS``, ``NEUTRAL``, ``SKIPPED`` or ``PENDING``
exits 1, and so does a check with no name or an unknown value. Pending checks
alone exit 3, and only all-green checks exit 0.

``backfill-opened OWNER`` reads the body of every pull request, open or not,
in every repository of OWNER, and adds each one to the ledger of every
``session=`` its Generated-by, Assisted-by or Opened-by lines name. It prints
a count per repository and is safe to run again. It warns when a list
reaches gh's ``--limit``, since the rest may be missing.
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
import tomllib
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast, override
from urllib.parse import quote

import yaml
from jsonschema import Draft202012Validator

from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import opened, secret_scan, size_gate
from tbhbdev.alpha.repo import git as repo_git
from tbhbdev.alpha.repo import hook_cwd, in_tbhb_dev, read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from jsonschema.exceptions import ValidationError

# Each `cast` below carries `pragma: no mutate`: it is a runtime no-op, so no test
# could tell a mutated type string from the original.
type Problem = tuple[int, str]

# Names live at module level, where mutmut leaves them alone.
DRAFT = Path(".github") / "PULL_REQUEST_DRAFT.md"
SCHEMA = Path(".github") / "schemas" / "pull-request-draft.schema.json"
SECTIONS = Path(".github") / "pull-request-sections.toml"
# Config files each linter reads, mirrored into the temp tree by symlink.
CONFIGS = (".vale.ini", ".vale", ".cspell.jsonc", ".cspell-words.txt", ".rumdl.toml")
# A merge appends " (#N)" and the header must fit 72; 64 leaves room
# for a pull request number of up to four digits.
MAX_TITLE = 72 - len(" (#9999)")
SCC = "scc"
BAZEL = "bazelisk"
BAZEL_ENV = ".bazel_env"
# Module constants, not literals in the functions: macOS file systems ignore
# case, so a mutant that changes a path's case there would pass every test.
VALE_INI = ".vale.ini"
STYLES = ".vale"
VALE_SYNC = "//:vale_sync"
# Vale's built-in style, which no package provides.
BUILTIN_STYLE = "Vale"
# The .vale.ini key that names styles, compared without regard to case.
BASED_ON = "BasedOnStyles"
GH = "gh"
RENOVATE_AUTHOR = "tbhb-dev-renovate[bot]"
RENOVATE_APP = "app/tbhb-dev-renovate"
REBASE_MARKER = "<!-- rebase-check -->"
REBASE_UNCHECKED = "- [ ]"
REBASE_CHECKED = "- [x]"
GIT = "git"
CLAUDE = "claude"
CODEX = "codex"
ANTIGRAVITY = "antigravity"
AGY_EDITS = agent_identity.AGY_EDIT_TOOLS
# The exit code that makes a harness feed a PostToolUse hook's stderr to the agent.
HOOK_FEEDBACK = 2
COG = "cog"
GITLEAKS = "gitleaks"
MISE = "mise"
_TOOL_PATHS: ContextVar[Mapping[tuple[str, bool], str]] = ContextVar(
    "_TOOL_PATHS", default=MappingProxyType({})
)
# The title is a commit header, so the sentence-case heading rule does not apply
# to line 1: a finding on it reads (1, "vale project.HeadingTitleCase: ...").
TITLE_ALERT = (1, "vale project.HeadingTitleCase")
HEADING = re.compile(r" {0,3}(#{1,6}) +(\S.*?) *")
FRONTMATTER = re.compile(r"---\n(?P<yaml>.*?)^---[ \t]*$\n?", re.DOTALL | re.MULTILINE)
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
INLINE_CODE = re.compile(r"(`+).+?\1")
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:.*$", re.MULTILINE)
URL = re.compile(r"<?https?://[^\s>]*>?")
RUMDL_LINE = re.compile(r"\S+?:(\d+):\d+: (.+)")
CSPELL_LINE = re.compile(r"\S+?:(\d+):\d+ - (.+)")
HUMAN_TRAILER = re.compile(r"(?:Signed-off-by|Co-authored-by):", re.IGNORECASE)
OPENED_KEY = "Opened-by"
# The line create writes, and any spelling of the key in a draft.
OPENED_LINE = re.compile(rf"{OPENED_KEY}: ")
DRAFT_OPENED = re.compile(r"\s*opened-by\s*:", re.IGNORECASE)
OPENED_MESSAGE = "the script writes Opened-by; remove this line"
PR_URL = re.compile(r"https://[^/\s]+/([^/\s]+/[^/\s]+)/pull/(\d+)")
# A repository as ``owner/name``: GitHub's characters for each part, and a name
# that is not "." or "..". The owner can't begin with "-", so gh can't read it
# as a flag.
REPO = r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.+(?:/|\Z))[A-Za-z0-9_.-]+"
NAME_WITH_OWNER = re.compile(REPO)
PR_PAGE = re.compile(rf"https://github\.com/({REPO})/pull/[0-9]+")
REPO_LIMIT = 1000
PR_LIMIT = 10000
EDIT_DRAFT = "run `tbhbdev alpha pr edit DRAFT --pr {number}` to apply the draft"
BACKFILL_KEYS = re.compile(r"(?:Generated-by|Assisted-by|Opened-by):\s*(LLM\b.*)")
SESSION_ATTR = re.compile(r"(?<![^\s;])session=(sha-256-64:[0-9a-f]{16})(?![^\s;])")
LIST_ITEM = re.compile(r" *(?:[-*+]|\d+[.)]) +\S")
# A ``#N`` that GitHub links to this repository's issue or pull request N. One
# written ``owner/repo#N`` has a name character before the ``#``.
BARE_REF = re.compile(r"(?<![\w./&-])#(\d+)\b")
BARE_REF_MESSAGE = (
    "#{number} links to this repository's #{number}; "
    "write owner/repo#{number}, even for this repository"
)
HUMAN_TRAILER_MESSAGE = (
    "Signed-off-by and Co-authored-by do not belong in the description; "
    "remove this line"
)
WRAPPED = (
    "paragraph is hard-wrapped; write each paragraph on one line "
    "(GitHub renders newlines as line breaks)"
)


@dataclass(frozen=True)
class Section:
    """One H2 section of the description, from the sections file."""

    heading: str
    required: bool
    max_words: int | None


@dataclass(frozen=True)
class Line:
    """One draft line: its number, its raw text, and what analysis sees of it.

    ``text`` has HTML comments removed and is empty inside a fenced block,
    where ``code`` is True.
    """

    no: int
    raw: str
    text: str
    code: bool


@dataclass(frozen=True)
class Block:
    """An H2 section: its heading line and the lines up to the next H2."""

    heading: str
    no: int
    lines: list[Line]


class FrontmatterError(ValueError):
    """The frontmatter block cannot be read."""


def keep_text(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    """Construct a timestamp as the text the author wrote."""
    return loader.construct_scalar(node)  # pyrefly: ignore[bad-argument-type]


class Loader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as text and refuses duplicate keys."""

    @override
    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        """Refuse a key that appears twice, which plain YAML resolves silently."""
        keys = [self.construct_object(key, deep=True) for key, _ in node.value]
        for key in keys:
            if keys.count(key) > 1:
                message = f"duplicate key {key!r}"
                raise yaml.constructor.ConstructorError(
                    None, None, message, node.start_mark
                )
        return super().construct_mapping(node, deep)


Loader.add_constructor("tag:yaml.org,2002:timestamp", keep_text)


def parse_frontmatter(text: str) -> tuple[object, str]:
    """Split a draft into its parsed frontmatter (None when absent) and body.

    Raises:
        FrontmatterError: The block is unterminated or is not valid YAML.
    """
    match = FRONTMATTER.match(text)
    if match is None:
        if text.startswith("---\n"):
            message = "unterminated frontmatter"
            raise FrontmatterError(message)
        return None, text
    try:
        # Loader is a SafeLoader.
        data: object = yaml.load(match["yaml"], Loader=Loader)  # noqa: S506  # nosec B506
    except yaml.YAMLError as exc:
        message = f"invalid YAML: {' '.join(str(exc).split())}"
        raise FrontmatterError(message) from exc
    return data, text[match.end() :]


@dataclass(frozen=True)
class Checked:
    """Everything ``check`` learned. ``rendered`` runs from the title line on."""

    problems: list[Problem]
    title: str = ""
    rendered: list[str] = field(default_factory=list)
    meta: dict[str, object] = field(default_factory=dict)


def git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run Git with the executable resolved during command startup, if any."""
    return repo_git(*args, cwd=cwd, executable=_TOOL_PATHS.get().get((GIT, False), GIT))


def root(cwd: Path | None = None) -> Path:
    """The top of the worktree containing ``cwd`` (default: the current one).

    Outside a worktree, the directory itself.
    """
    code, out = git("rev-parse", "--show-toplevel", cwd=cwd)
    return Path(out) if code == 0 else (cwd or Path.cwd())


def read_baseline(path: Path) -> str:
    """Read one of the shared baseline files from the worktree.

    Raises:
        SystemExit: The file is missing.
    """
    try:
        return (root() / path).read_text()
    except FileNotFoundError:
        message = (
            f"{path} is missing: this repository needs the shared baseline"
            " from tbhb-dev/rules"
        )
        raise SystemExit(message) from None


def load_sections() -> list[Section]:
    """Read the section scheme."""
    data = tomllib.loads(read_baseline(SECTIONS))
    rows = cast("list[dict[str, object]]", data["section"])  # pragma: no mutate
    return [
        Section(
            str(row["heading"]),
            row["required"] is True,
            cast("int | None", row.get("max_words")),  # pragma: no mutate
        )
        for row in rows
    ]


def load_schema() -> dict[str, object]:
    """Read the frontmatter schema."""
    text = read_baseline(SCHEMA)
    return cast("dict[str, object]", json.loads(text))  # pragma: no mutate


def schema_default(schema: Mapping[str, object], name: str) -> object:
    """The default the schema declares for a property."""
    props = cast(
        "dict[str, dict[str, object]]", schema["properties"]
    )  # pragma: no mutate
    return props[name]["default"]


def count_words(text: str) -> int:
    """Count prose words: code, link targets, and comments are free.

    A word is a whitespace-separated token with a letter or digit in it, so
    list markers and bare punctuation are not words.
    """
    text = COMMENT.sub(" ", text)
    text = INLINE_CODE.sub(" ", text)
    text = REF_DEF.sub(" ", text)
    text = LINK.sub(r" \1 ", text)
    text = REF_LINK.sub(r" \1 ", text)
    text = URL.sub(" ", text)
    return sum(any(char.isalnum() for char in token) for token in text.split())


def fence_flags(raw: Sequence[str]) -> tuple[list[bool], int | None]:
    """Flag the lines inside fenced code, delimiters included.

    Also returns the index of the opening fence that is never closed, if any.
    """
    flags: list[bool] = []
    marker = ""  # pragma: no mutate -- None is as falsy
    opened = 0  # pragma: no mutate -- read only once marker is set
    for index, line in enumerate(raw):
        match = FENCE.match(line)
        if not marker:
            if match:
                marker, opened = match[1], index
            flags.append(bool(marker))
            continue
        flags.append(True)
        if match and set(match[1]) == set(marker) and len(match[1]) >= len(marker):
            marker = "" if line.strip() == match[1] else marker
    return flags, (opened if marker else None)


def strip_comments(texts: Sequence[str]) -> tuple[list[str], list[int]]:
    """Blank out HTML comments, keeping line numbers.

    Returns the lines and the index of the line each comment starts on.
    """
    joined = "\n".join(texts)
    starts = [joined[: match.start()].count("\n") for match in COMMENT.finditer(joined)]
    clean = COMMENT.sub(lambda match: "\n" * match[0].count("\n"), joined)
    return clean.split("\n")[: len(texts)], starts


def scan(raw: Sequence[str], first: int) -> tuple[list[Line], list[Problem]]:
    """Turn raw body lines (the first is line ``first``) into analysis lines."""
    flags, unclosed = fence_flags(raw)
    texts, comments = strip_comments(
        ["" if flags[index] else line for index, line in enumerate(raw)]
    )
    problems = [
        (first + index, "HTML comment left in the description") for index in comments
    ]
    if unclosed is not None:
        problems.append((first + unclosed, "code fence is never closed"))
    lines = [
        Line(first + index, line, texts[index], flags[index])
        for index, line in enumerate(raw)
    ]
    return lines, problems


def heading_of(line: Line) -> tuple[int, str] | None:
    """The level and text of a heading line, or None."""
    match = None if line.code else HEADING.fullmatch(line.text)
    return (len(match[1]), match[2]) if match else None


def split_blocks(
    lines: Sequence[Line],
) -> tuple[list[Line], list[Block], list[Problem]]:
    """Split the lines after the title into the preamble and the H2 sections."""
    preamble: list[Line] = []
    blocks: list[Block] = []
    problems: list[Problem] = []
    for line in lines:
        found = heading_of(line)
        if found and found[0] == 1:
            problems.append((line.no, "only one H1 is allowed: the title"))
        if found and found[0] == 2:  # noqa: PLR2004 -- H2 is the section level
            blocks.append(Block(found[1], line.no, []))
        elif blocks:
            blocks[-1].lines.append(line)
        else:
            preamble.append(line)
    return preamble, blocks, problems


def is_blank(line: Line) -> bool:
    """True for a line with no text and no code."""
    return not line.code and not line.text.strip()


def block_problems(
    blocks: Sequence[Block], sections: Sequence[Section]
) -> list[Problem]:
    """Check section names, order, emptiness, and word limits."""
    order = {section.heading: index for index, section in enumerate(sections)}
    problems: list[Problem] = []
    last = -1  # pragma: no mutate -- indexes start at 0, so -2 behaves alike
    for block in blocks:
        index = order.get(block.heading)
        if index is None:
            known = ", ".join(order)
            problems.append(
                (block.no, f"unknown section '{block.heading}'; use: {known}")
            )
            continue
        if index <= last:
            problems.append(
                (block.no, f"section '{block.heading}' is duplicated or out of order")
            )
        last = max(last, index)
        if all(map(is_blank, block.lines)):
            problems.append((block.no, f"section '{block.heading}' is empty"))
        limit = sections[index].max_words
        words = count_words("\n".join(line.text for line in block.lines))
        if limit is not None and words > limit:
            problems.append(
                (
                    block.no,
                    f"section '{block.heading}' has {words} words (limit {limit})",
                )
            )
    return problems


def wrap_problems(lines: Sequence[Line]) -> list[Problem]:
    """Flag each paragraph or list item that continues onto a second line.

    GitHub renders a newline in a description as a line break, so wrapped prose
    shows as ragged short lines. Code, tables, headings, blank lines, and the
    start of a list item end a run, so each list item stays on its own line.
    """
    problems: list[Problem] = []
    start: Line | None = None  # first line of the prose run being read
    reported = False
    for line in lines:
        text = "" if line.code else line.text.strip()
        if (
            not text
            or HEADING.fullmatch(line.text)
            or text.startswith("|")
            or HUMAN_TRAILER.match(text)
            or agent_identity.LINE.match(text)
        ):
            start = None
        elif start is None or LIST_ITEM.match(line.text):
            start, reported = line, False
        elif not reported:
            problems.append((start.no, WRAPPED))
            reported = True
    return problems


def trailer_problems(lines: Sequence[Line]) -> list[Problem]:
    """Reject trailers in the draft: the script writes the LLM ones.

    Signed-off-by and Co-authored-by are rejected for everyone: the merge
    commit's author is the person who merges, and the script never writes them.
    """
    problems: list[Problem] = []
    for line in lines:
        if DRAFT_OPENED.match(line.raw):
            problems.append((line.no, OPENED_MESSAGE))
            continue
        if line.code:
            continue
        if agent_identity.LINE.match(line.text):
            problems.append(
                (
                    line.no,
                    "the script writes Generated-by and Assisted-by; remove this line",
                )
            )
        elif HUMAN_TRAILER.match(line.text):
            problems.append((line.no, HUMAN_TRAILER_MESSAGE))
    return problems


def ref_problems(lines: Sequence[Line]) -> list[Problem]:
    """Reject ``#N`` references that don't name their repository.

    GitHub links a bare ``#N`` to the repository it appears in, which is wrong
    for another repository's pull request and ambiguous once the merge commit
    is read elsewhere, so every reference is ``owner/repo#N``. Code and links
    are exempt: neither becomes a reference.
    """
    problems: list[Problem] = []
    for line in lines:
        if line.code:
            continue
        text = INLINE_CODE.sub(" ", line.text)
        text = LINK.sub(" ", text)
        text = REF_LINK.sub(" ", text)
        text = URL.sub(" ", text)
        problems += [
            (line.no, BARE_REF_MESSAGE.format(number=number))
            for number in BARE_REF.findall(text)
        ]
    return problems


def meta_problems(
    data: object, offset: int, text: str, validator: Draft202012Validator
) -> list[Problem]:
    """Validate the frontmatter, pointing each error at its key's line."""
    if data is None:
        return []
    frontmatter = text.splitlines()[:offset]
    return [
        (
            key_line(error, frontmatter),
            f"frontmatter {error.json_path}: {error.message}",
        )
        for error in sorted(
            validator.iter_errors(data), key=lambda found: found.json_path
        )
    ]


def key_line(error: ValidationError, frontmatter: Sequence[str]) -> int:
    """The line of the top-level key an error is about; 1 when it has none."""
    if not error.absolute_path:
        return 1
    key = f"{error.absolute_path[0]}:"
    return next(
        (index for index, line in enumerate(frontmatter, 1) if line.startswith(key)), 1
    )


def find_tool(name: str, *, unwrapped: bool = False) -> str:
    """Find a tool on PATH, in the worktree's Bazel shims, or through mise.

    ``unwrapped`` skips the Bazel shims in ``.bazel_env``. For gitleaks the shim
    is the rules wrapper, which refuses a config with ``[extend]``, and the text
    scan writes one that has it, so the scan needs the real binary.
    """
    found = _TOOL_PATHS.get().get((name, unwrapped)) or _locate_tool(
        name, unwrapped=unwrapped
    )
    if found:
        return found
    message = f"{name} not found on PATH or through `mise which`; run `mise install`"
    raise SystemExit(message)


def _locate_tool(name: str, *, unwrapped: bool) -> str | None:
    """Return the executable path, or None when all lookups fail."""
    path = None
    if unwrapped:
        entries = os.environ.get("PATH", "").split(os.pathsep)
        path = os.pathsep.join(e for e in entries if BAZEL_ENV not in e)
    found = shutil.which(name, path=path)
    if found:
        return found
    if not unwrapped and (shim := shutil.which(str(root() / BAZEL_ENV / "bin" / name))):
        return shim
    mise = shutil.which(MISE, path=path)
    if mise:
        done = run([mise, "which", name])
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return None


def find_tools(
    names: Sequence[str], *, unwrapped: Collection[str] = ()
) -> dict[str, str]:
    """Resolve each tool or report all missing tools together."""
    paths: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        if found := _locate_tool(name, unwrapped=name in unwrapped):
            paths[name] = found
        else:
            missing.append(name)
    if missing:
        message = (
            f"missing tools: {', '.join(missing)}; "
            "run `bazelisk run //:bazel_env` or `mise install`"
        )
        raise SystemExit(message)
    return paths


def run(
    argv: Sequence[str], *, cwd: Path | None = None, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured output.

    Stdin is a pipe, never a terminal, so gh cannot prompt, and a prompt could
    push a branch.
    """
    # Argv comes from fixed names and validated draft fields.
    return subprocess.run(  # nosec B603
        list(argv),
        capture_output=True,
        text=True,
        check=False,  # pragma: no mutate -- the default, spelled for the linter
        cwd=cwd,
        input=stdin,
    )


def title_problems(title: str, limit: int = MAX_TITLE) -> list[str]:
    """Why a title is not a Conventional Commit header that fits limit."""
    problems = []
    if len(title) > limit:
        problems.append(f"title is {len(title)} characters; the limit is {limit}")
    done = run([find_tool(COG), "verify", "--file", "-"], cwd=root(), stdin=title)
    if done.returncode != 0:
        why = " ".join(done.stderr.split())
        problems.append(
            f"title is not a Conventional Commit header (cog verify): {why}"
        )
    return problems


def parse_vale(output: str) -> list[Problem]:
    """Vale's JSON alerts."""
    try:
        data = cast(
            "dict[str, list[dict[str, object]]]", json.loads(output)
        )  # pragma: no mutate
    except ValueError:
        return []
    return [
        (int(str(alert["Line"])), f"vale {alert['Check']}: {alert['Message']}")
        for alerts in data.values()
        for alert in alerts
    ]


def parse_lines(pattern: re.Pattern[str], tool: str) -> Callable[[str], list[Problem]]:
    """A parser for a tool that prints one `path:line:col...` finding per line."""

    def parse(output: str) -> list[Problem]:
        found = (pattern.fullmatch(line) for line in output.splitlines())
        return [(int(match[1]), f"{tool}: {match[2]}") for match in found if match]

    return parse


LINTERS: tuple[tuple[str, tuple[str, ...], Callable[[str], list[Problem]]], ...] = (
    ("vale", ("--output=JSON",), parse_vale),
    (
        "cspell",
        ("--config", ".cspell.jsonc", "--no-summary", "--no-progress"),
        parse_lines(CSPELL_LINE, "cspell"),
    ),
    ("rumdl", ("check",), parse_lines(RUMDL_LINE, "rumdl")),
)


def vale_styles(top: Path) -> list[str]:
    """The styles .vale.ini's ``BasedOnStyles`` lines name, in any section."""
    named: list[str] = []
    for line in (top / VALE_INI).read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == BASED_ON.lower():
            named += (name.strip() for name in value.split(","))
    return sorted({name for name in named if name and name != BUILTIN_STYLE})


def missing_styles(top: Path) -> list[str]:
    """The named styles with no directory under .vale/."""
    return [name for name in vale_styles(top) if not (top / STYLES / name).is_dir()]


def style_problem(top: Path) -> str | None:
    """Sync missing vale styles once; why vale can't run, or None."""
    missing = missing_styles(top)
    if not missing:
        return None
    bazel = find_tool(BAZEL)
    target = run([bazel, "query", VALE_SYNC], cwd=top)
    if target.returncode != 0 or VALE_SYNC not in target.stdout.splitlines():
        return (
            f"vale styles missing from .vale/: {', '.join(missing)}; "
            f"missing Bazel target {VALE_SYNC}"
        )
    done = run([bazel, "run", VALE_SYNC], cwd=top)
    still = missing_styles(top)
    if not still:
        return None
    why = " ".join((done.stderr or done.stdout).split())
    return (
        f"vale styles missing from .vale/: {', '.join(still)}; "
        f"`{BAZEL} run {VALE_SYNC}` exited {done.returncode}: {why}"
    )


def lint(
    rendered: Sequence[str],
    first: int,
    *,
    path: Path = DRAFT,
    drop: frozenset[tuple[int, str]] = frozenset({TITLE_ALERT}),
) -> list[Problem]:
    """Lint the title and description with the repository's own configs.

    The text goes into a temp tree with the draft's relative path and symlinks
    to the configs, so the globs in those configs match. Rendered line N is
    draft line ``first + N - 1``. ``drop`` matches a line and either a rule
    prefix or its full diagnostic.
    """
    tools = [(name, find_tool(name), args, parse) for name, args, parse in LINTERS]
    top = root()
    for name in CONFIGS:
        if not (top / name).exists():
            message = f"missing lint config {name} in {top}"
            raise SystemExit(message)
    styles = style_problem(top)
    problems: list[Problem] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for name in CONFIGS:
            (work / name).symlink_to(top / name)
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text("\n".join(rendered) + "\n")
        for name, exe, args, parse in tools:
            if name == "vale" and styles:
                problems.append((first, f"vale: {styles}"))
                continue
            problems.extend(
                (first + line - 1, text)
                for line, text in run_linter(name, [exe, *args], parse, work, path)
                if (line, text.split(":")[0]) not in drop and (line, text) not in drop
            )
    return problems


def run_linter(
    name: str,
    argv: list[str],
    parse: Callable[[str], list[Problem]],
    work: Path,
    path: Path = DRAFT,
) -> list[Problem]:
    """Run one linter on ``path`` in ``work``; its findings, or why it failed."""
    done = run([*argv, str(path)], cwd=work)
    found = parse(done.stdout)
    # Exit 1 means findings in all three tools; anything else, or
    # exit 1 with nothing parsed, is the tool failing.
    if done.returncode not in {0, 1} or (done.returncode == 1 and not found):
        why = " ".join((done.stderr or done.stdout).split())
        found = [(1, f"{name} failed (exit {done.returncode}): {why}")]
    return found


def body_problems(
    lines: Sequence[Line], anchor: Line, sections: Sequence[Section] | None = None
) -> list[Problem]:
    """Check sections, stray text, and trailers; anchor missing sections here."""
    preamble, blocks, problems = split_blocks(lines)
    stray = next((line for line in preamble if not is_blank(line)), None)
    if stray:
        problems.append(
            (stray.no, "text before the first section; put it under a heading")
        )
    if sections is None:
        sections = load_sections()
    problems += block_problems(blocks, sections)
    problems += wrap_problems(lines)
    present = {block.heading for block in blocks}
    problems += [
        (anchor.no, f"missing required section '{section.heading}'")
        for section in sections
        if section.required and section.heading not in present
    ]
    return problems + trailer_problems(lines)


def check_text(text: str) -> Checked:
    """Validate and lint a draft. The linters run only once there is a title."""
    try:
        data, body = parse_frontmatter(text)
    except FrontmatterError as exc:
        return Checked([(1, f"frontmatter: {exc}")])
    offset = text.count("\n") - body.count("\n")
    problems = meta_problems(data, offset, text, Draft202012Validator(load_schema()))
    meta = data if isinstance(data, dict) and not problems else {}
    lines, scanned = scan(body.splitlines(), offset + 1)
    problems += scanned
    start = next(
        (index for index, line in enumerate(lines) if not is_blank(line)), None
    )
    found = None if start is None else heading_of(lines[start])
    if start is None or found is None or found[0] != 1:
        where = offset + 1 if start is None else lines[start].no
        problems.append(
            (where, "the first line after the frontmatter must be the H1 title")
        )
        return Checked(sorted(problems))
    title = lines[start]
    problems += [(title.no, why) for why in title_problems(found[1])]
    problems += body_problems(lines[start + 1 :], title)
    problems += ref_problems(lines[start:])
    rendered = [line.raw for line in lines[start:]]
    problems += lint(rendered, title.no)
    return Checked(sorted(set(problems)), found[1], rendered, meta)


def body_of(rendered: Sequence[str]) -> str:
    """The description: everything after the title, trimmed."""
    return "\n".join(rendered[1:]).strip("\n") + "\n"


def identity_key(value: str) -> tuple[str, ...]:
    """Who an LLM trailer value names: harness slug, model, session, subagent."""
    blob = value.split()[1]
    attrs = {
        key: val for key, _, val in (pair.partition("=") for pair in blob.split(";"))
    }
    return (
        attrs["harness"].partition(":")[0],
        attrs.get("model", ""),
        attrs.get("session", ""),
        attrs.get("subagent", ""),
    )


def merge_trailers(
    found: Sequence[tuple[str, str]], posting: str | None
) -> list[tuple[str, str]]:
    """Deduplicate (key, value) trailers by identity; Generated-by wins.

    The posting agent's value is added as Assisted-by when its identity is
    absent. Generated-by lines come first, as the commit-msg check requires.
    """
    roles: dict[tuple[str, ...], tuple[str, str]] = {}
    for key, value in found:
        ident = identity_key(value)
        if ident not in roles or (
            key == agent_identity.KEY_GEN and roles[ident][0] != key
        ):
            roles[ident] = (key, value)
    if posting is not None and identity_key(posting) not in roles:
        roles[identity_key(posting)] = (agent_identity.KEY_ASSIST, posting)
    return sorted(roles.values(), key=lambda pair: pair[0] != agent_identity.KEY_GEN)


def branch_trailers(base: str) -> list[tuple[str, str]]:
    """The LLM trailers of the commits on this branch, oldest first.

    The range is BASE..HEAD, which starts at the merge base. The PR targets the
    remote branch, so refs/remotes/origin/BASE is the base when it exists, else
    refs/heads/BASE. Full ref names keep a tag named origin/BASE out. Git runs in
    the current directory, because the branch belongs to the repository the
    script is run from.
    """
    remote = f"refs/remotes/origin/{base}"
    code, _ = git("rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}")
    span = f"{remote if code == 0 else f'refs/heads/{base}'}..HEAD"
    code, out = git("log", "--reverse", "--format=%x1e%(trailers:only,unfold)", span)
    if code != 0:
        message = f"cannot list the commits in {base}..HEAD; is {base} fetched?"
        raise SystemExit(message)
    return trailer_lines(out)


def trailer_lines(text: str) -> list[tuple[str, str]]:
    """Match LLM trailer lines in Git's parsed trailer output."""
    found = (agent_identity.LINE.match(line) for line in text.splitlines())
    return [(match[1], match[2]) for match in found if match]


def commit_messages(pages: Sequence[object], invalid: str) -> list[str]:
    """Read the commit messages in the order GitHub returned them."""
    messages: list[str] = []
    for page in pages:
        if not isinstance(page, list):
            raise SystemExit(invalid)
        for entry in cast("list[object]", page):  # pragma: no mutate
            item = agent_identity.as_dict(entry) or {}
            commit = agent_identity.as_dict(item.get("commit")) or {}
            message = commit.get("message")
            if not isinstance(message, str):
                raise SystemExit(invalid)
            messages.append(message)
    return messages


def pr_trailers(number: int, target: str) -> list[tuple[str, str]]:
    """The LLM trailers of the commits GitHub lists for the pull request."""
    problem = f"cannot list the commits of PR {number}"
    if not target:
        error = f"{problem}: cannot determine its GitHub repository"
        raise SystemExit(error)
    endpoint = f"repos/{target}/pulls/{number}/commits"
    api = [find_tool(GH), "api", "--hostname", "github.com", "--paginate", "--slurp"]
    done = run([*api, endpoint])
    pages = gh_data(done)
    if not isinstance(pages, list):
        why = " ".join(done.stderr.split()) or "GitHub returned no commit list"
        error = f"{problem}: {why}"
        raise SystemExit(error)
    pages = cast("list[object]", pages)  # pragma: no mutate
    invalid = f"{problem}: GitHub returned invalid commit data"
    messages = commit_messages(pages, invalid)
    if not messages:
        error = f"{problem}: GitHub returned no commits"
        raise SystemExit(error)
    argv = [find_tool(GIT), "interpret-trailers", "--parse", "--no-divider"]
    found: list[tuple[str, str]] = []
    for message in messages:
        parsed = run(argv, stdin=message)
        if parsed.returncode != 0:
            error = f"cannot parse the commit trailers of PR {number}"
            raise SystemExit(error)
        found.extend(trailer_lines(parsed.stdout))
    return found


def posting_value(env: Mapping[str, str]) -> str | None:
    """The trailer value for the agent running this script, or None for a human."""
    committer = agent_identity.committer_identity(env)
    if committer is None:
        return None
    return agent_identity.format_value(committer.ident.attrs())


def default_base() -> str:
    """The repository's default branch: the remote's own HEAD, else origin/HEAD.

    The remote answers first because origin/HEAD goes stale when the default
    branch moves and a plain fetch leaves it, and a fresh mirror may have none.

    Raises:
        SystemExit: ``GH_REPO`` names a repository that origin does not describe,
            or neither read answers. No guess stands in, so a draft never
            targets a wrong base.
    """
    hint = "; name the base in the draft's frontmatter"
    if os.environ.get("GH_REPO"):
        message = f"cannot find the default branch of GH_REPO{hint}"
        raise SystemExit(message)
    code, out = git("ls-remote", "--symref", "origin", "HEAD")
    found = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", out, re.MULTILINE)
    if code == 0 and found:
        return found[1]
    code, out = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    name = out.removeprefix("refs/remotes/origin/")
    if code == 0 and name and name != out:
        return name
    message = (
        "cannot find the default branch: `git ls-remote --symref origin HEAD` "
        f"failed and origin/HEAD is not a symbolic ref{hint}"
    )
    raise SystemExit(message)


def base_of(meta: Mapping[str, object], default: str | None = None) -> str:
    """The PR's base branch: the frontmatter's, else DEFAULT or the repository's."""
    if "base" in meta:
        return str(meta["base"])
    return default if default is not None else default_base()


def gh_data(
    done: subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes],
) -> object:
    """The JSON that gh printed, or None when gh failed or printed none."""
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def live_pr(number: int) -> tuple[str | None, object]:
    """PR N's ``Opened-by`` line on GitHub, or None, and its ``url``.

    The line is the one in the body's final paragraph. The URL is whatever gh
    reports, None when it reports none; ``target_of_pr`` takes it as it is.

    Raises:
        SystemExit: gh can't read the body, so the line can't be kept.
    """
    argv = [find_tool(GH), "pr", "view", str(number), "--json", "body,url"]
    done = run(argv)
    data = gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read the body of PR {number} to keep its Opened-by: {why}"
        raise SystemExit(message)
    line = opened_line(str(data.get("body") or ""))  # pragma: no mutate -- no line
    return line, data.get("url")


def opened_line(body: str) -> str | None:
    """The ``Opened-by`` line of a PR body's final paragraph, or None."""
    paragraph: list[str] = []
    for line in reversed(body.splitlines()):
        if line.strip():
            paragraph.append(line)
        elif paragraph:
            break
    return next((line for line in paragraph if OPENED_LINE.match(line)), None)


def description(
    checked: Checked,
    env: Mapping[str, str],
    found: Sequence[tuple[str, str]],
    opened_by: str | None = None,
) -> str:
    """The final body: the description plus the trailers as its last paragraph.

    ``opened_by``, when given, is the paragraph's first line.

    Raises:
        SystemExit: A trailer, or the posting agent's, fails the commit-trailer
            grammar.
    """
    posting = posting_value(env)
    values = [value for _, value in found] + ([posting] if posting else [])
    errors = [
        f"{value}: {why}"
        for value in values
        if (why := agent_identity.check_value(value))
    ]
    if errors:
        raise SystemExit("invalid LLM trailer: " + "; ".join(errors))
    merged = [f"{key}: {value}" for key, value in merge_trailers(found, posting)]
    trailers = ([opened_by] if opened_by else []) + merged
    body = body_of(checked.rendered)
    if not trailers:
        return body
    return body + "\n" + "\n".join(trailers) + "\n"


def target_of_pr(url: object) -> str:
    """The ``owner/name`` in a pull request's github.com URL, or "".

    The URL must be a string of exactly
    ``https://github.com/<owner>/<name>/pull/<digits>``; anything else, a
    missing URL included, names nothing.
    """
    found = PR_PAGE.fullmatch(url) if isinstance(url, str) else None
    return found[1] if found else ""


def repo_visibility(target: str) -> str:
    """The visibility of ``target``, an ``owner/name``, as gh reports it.

    This is the visibility of the repository the text is posted to, not the
    working directory's. An empty, missing or malformed target returns ""
    without a gh call, and so does a failed read. An empty value scans under the
    full private config.
    """
    if not NAME_WITH_OWNER.fullmatch(target):
        return ""
    argv = [
        find_tool(GH),
        "repo",
        "view",
        target,
        "--json",
        "visibility",
        "--jq",
        ".visibility",
    ]
    done = run(argv)
    return done.stdout.strip() if done.returncode == 0 else ""


def base_visibility() -> str:
    """The visibility of the repository ``gh pr create`` will post to, or "".

    No pull request exists yet, so this is the base repository. gh resolves it
    for ``gh repo view`` with no argument the same way it does for
    ``gh pr create``, so one read of ``nameWithOwner`` and ``visibility`` gives
    both. The visibility counts only if ``nameWithOwner`` parses as
    ``owner/name``; a failed or malformed read returns "".
    """
    done = run([find_tool(GH), "repo", "view", "--json", "nameWithOwner,visibility"])
    data = gh_data(done)
    if not isinstance(data, dict):
        return ""
    name = data.get("nameWithOwner")
    if not (isinstance(name, str) and NAME_WITH_OWNER.fullmatch(name)):
        return ""
    return str(data.get("visibility") or "")


def scan_secrets(text: str, what: str, visibility: str) -> None:
    """Refuse text that gitleaks flags; see ``tbhbdev.alpha.secret_scan``.

    ``visibility`` is that of the repository the text is posted to, which the
    caller reads once per command, with ``repo_visibility`` or
    ``base_visibility``.
    """
    secret_scan.scan(
        text,
        what,
        root=root(),
        gitleaks=find_tool(GITLEAKS, unwrapped=True),
        visibility=visibility,
    )


def post_text(title: str, body: str, visibility: str) -> None:
    """Scan the PR title and body as gh would post them."""
    scan_secrets(f"{title}\n\n{body}", "the PR title and body", visibility)


def gh_args(meta: Mapping[str, object]) -> list[str]:
    """The gh pr create flags the frontmatter asks for."""
    args = ["--base", base_of(meta)]
    if meta.get("draft"):
        args.append("--draft")
    for flag, key in (
        ("--label", "labels"),
        ("--reviewer", "reviewers"),
        ("--assignee", "assignees"),
    ):
        names = cast("list[str]", meta.get(key, []))  # pragma: no mutate
        args += [item for name in names for item in (flag, name)]
    if "milestone" in meta:
        args += ["--milestone", str(meta["milestone"])]
    return args


def load_checked(path: Path) -> Checked:
    """Check a draft file."""
    try:
        text = path.read_text()
    except OSError as exc:
        return Checked([(1, f"cannot read the draft: {exc.strerror}")])
    return check_text(text)


def read_checked(path: Path) -> Checked:
    """Check a draft file and print its problems."""
    checked = load_checked(path)
    for line, why in checked.problems:
        print(f"{path}:{line}: {why}")
    return checked


def invoke_gh(
    argv: list[str], body: str, *, dry_run: bool = False, quiet: bool = False
) -> tuple[int, str]:
    """Run gh, or show what would run. Pass output and errors through unless quiet.

    Returns gh's exit code and its standard output.
    """
    if dry_run:
        print(shlex.join(argv))
        print()
        print(body, end="")
        return 0, ""
    argv[0] = find_tool(GH)
    done = run(argv, stdin=body)
    if not quiet:
        print(done.stdout, end="")
        print(done.stderr, end="", file=sys.stderr)
    return done.returncode, done.stdout


def origin_repository() -> str:
    """Resolve origin explicitly, independent of gh's default repository.

    Raises:
        SystemExit: origin or its GitHub repository can't be read.
    """
    code, url = git("remote", "get-url", "--push", "origin")
    if code or not url:
        message = "cannot read origin"
        raise SystemExit(message)
    done = run([find_tool(GH), "repo", "view", url, "--json", "nameWithOwner,url"])
    data = gh_data(done)
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or not isinstance(name, str)
        or not NAME_WITH_OWNER.fullmatch(name)
    ):
        message = "cannot resolve origin's GitHub repository"
        raise SystemExit(message)
    if data.get("url") != f"https://github.com/{name}":
        message = "origin must name a repository on github.com"
        raise SystemExit(message)
    return name


def target_is_origin() -> bool:
    """Whether ``gh pr create`` posts to origin's repository.

    gh's default repository (``gh repo set-default``, or a parent fork) may
    differ from origin, and ``GH_REPO`` overrides both. Any failed read counts
    as "no", so the size count is skipped instead of run against the wrong base.
    """
    if os.environ.get("GH_REPO"):
        return False  # gh pr create posts to GH_REPO, not to its default repository
    data = gh_data(run([find_tool(GH), "repo", "view", "--json", "nameWithOwner"]))
    target = data.get("nameWithOwner") if isinstance(data, dict) else None
    try:
        origin = origin_repository()
    except SystemExit:
        return False
    return isinstance(target, str) and target.casefold() == origin.casefold()


def closed_pr_head(number: int, repo: str) -> str:
    """Read the head of a finished PR whose branch belongs to origin.

    Raises:
        SystemExit: The PR can't be read, isn't finished or has an unsafe head.
    """
    done = run(
        [
            find_tool(GH),
            "pr",
            "view",
            str(number),
            "--repo",
            f"github.com/{repo}",
            "--json",
            "state,headRefName,isCrossRepository",
        ]
    )
    data = gh_data(done)
    if not isinstance(data, dict):
        message = f"cannot read PR {number} on origin"
        raise SystemExit(message)
    data = cast("dict[str, object]", data)  # pragma: no mutate
    if data.get("state") not in ("CLOSED", "MERGED"):
        message = f"PR {number} is not closed or merged"
        raise SystemExit(message)
    if data.get("isCrossRepository") is not False:
        message = f"PR {number} head is not on origin"
        raise SystemExit(message)
    branch = data.get("headRefName")
    if not isinstance(branch, str) or not branch:
        message = f"cannot read the head branch of PR {number}"
        raise SystemExit(message)
    if branch == "main":
        message = "refusing to delete main"
        raise SystemExit(message)
    code, _ = git("check-ref-format", f"refs/heads/{branch}")
    if code:
        message = f"invalid head branch of PR {number}"
        raise SystemExit(message)
    return branch


SECRET_SHAPED = re.compile(r"\S+://\S+|\b[A-Za-z0-9_-]{20,}\b")
WHY_LIMIT = 200


def refusal_reason(*raw: bytes) -> str:
    """The captured stderr, with URLs and long token-like words redacted and cut."""
    # Python spells the codec error handler case-blind, so no test can tell "REPLACE".
    text = " ".join(
        b" ".join(raw).decode(errors="replace").split()  # pragma: no mutate
    )
    return SECRET_SHAPED.sub("<redacted>", text)[:WHY_LIMIT]


def run_bytes(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a command and keep its output as bytes, so line endings stay as written."""
    # Argv comes from fixed names and a validated pull request number.
    return subprocess.run(  # nosec B603
        list(argv),
        capture_output=True,
        check=False,  # pragma: no mutate -- the default, spelled for the linter
        # An inherited stdin behaves like the empty pipe for these gh reads.
        input=b"",  # pragma: no mutate
    )


DRIVER_APP_AUTHOR = "app/tbhb-dev-agent-pull-requests"


def require_own_author(number: int, repo: str) -> None:
    """Refuse unless PR N's author is this ``gh`` account or the driver's App.

    The driver opens a run's pull request as the pull-requests App, so no agent
    account authors it. Any other author, a human's included, is refused.

    Both reads name github.com and ``repo``, the repository ``gh pr ready``
    acts on, so no ``GH_HOST`` or ``GH_REPO`` setting can split them. The two
    logins must match byte for byte. The check isn't atomic with ``gh pr ready``.

    Raises:
        SystemExit: The author or the account can't be read, or they differ.
    """
    author = run_bytes(
        [
            find_tool(GH),
            "pr",
            "view",
            str(number),
            "--repo",
            f"github.com/{repo}",
            "--json",
            "author",
            "--jq",
            ".author.login",
        ]
    )
    me = run_bytes(
        [find_tool(GH), "api", "--hostname", "github.com", "user", "--jq", ".login"]
    )
    if (
        author.returncode
        or me.returncode
        or not author.stdout.strip()
        or (
            me.stdout != author.stdout
            and author.stdout.strip() != DRIVER_APP_AUTHOR.encode()
        )
    ):
        why = refusal_reason(author.stderr, me.stderr)
        message = (
            f"not marking PR {number} ready: its author isn't this agent's account"
        )
        raise SystemExit(f"{message}: {why}" if why else message)


def ready(numbers: Sequence[int]) -> int:
    """Mark each pull request ready, in order, stopping at the first failure.

    Raises:
        SystemExit: A pull request's author isn't this agent's account.
    """
    repo = origin_repository()
    for number in numbers:
        require_own_author(number, repo)
        # `gh pr ready` never reads stdin, so an inherited stdin behaves like the
        # empty body: no test can tell `None` from `""`.
        words = [GH, "pr", "ready", str(number), "--repo", f"github.com/{repo}"]
        status = invoke_gh(words, "")[0]  # pragma: no mutate
        if status:
            return status
    return 0


def renovate_author(login: object, number: int, repo: str) -> bool:
    """Check the exact login, resolving gh's display name for the App."""
    if login == RENOVATE_AUTHOR:
        return True
    if login != RENOVATE_APP:
        return False
    done = run_bytes(
        [
            find_tool(GH),
            "api",
            "--hostname",
            "github.com",
            f"repos/{repo}/pulls/{number}",
            "--jq",
            ".user.login",
        ]
    )
    return done.returncode == 0 and done.stdout == RENOVATE_AUTHOR.encode() + b"\n"


def tick_rebase_checkbox(body: str, line: str) -> str:
    """Tick the first checkbox on the unique line containing the marker."""
    return body.replace(line, line.replace(REBASE_UNCHECKED, REBASE_CHECKED, 1))


def rebase_body(number: int, body: str) -> str | None:
    """Check the marker and verify the rewritten body, including line endings.

    Raises:
        SystemExit: The checkbox is ambiguous, missing, or unsafe to change.
    """
    lines = body.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if REBASE_MARKER in line]
    if not matches:
        msg = f"PR {number}: no Renovate rebase checkbox line"
        raise SystemExit(msg)
    if len(matches) > 1:
        msg = f"PR {number}: multiple Renovate rebase checkbox lines"
        raise SystemExit(msg)
    (index,) = matches
    line = lines[index]
    if line.startswith(REBASE_CHECKED):
        return None
    if not line.startswith(REBASE_UNCHECKED):
        msg = f"PR {number}: malformed Renovate rebase checkbox line"
        raise SystemExit(msg)
    updated = tick_rebase_checkbox(body, line)
    # The prefix through the changed line and the suffix cover the entire body.
    prefix = "".join(lines[:index]) + REBASE_CHECKED + line[len(REBASE_UNCHECKED) :]
    if (
        len(updated) != len(body)
        or not updated.startswith(prefix)
        or not updated.endswith("".join(lines[index + 1 :]))
    ):
        msg = f"PR {number}: cannot verify a checkbox-only change"
        raise SystemExit(msg)
    return updated


def rebase(numbers: Sequence[int]) -> int:
    """Ask Renovate to rebase each open pull request, stopping at the first failure.

    Raises:
        SystemExit: The pull request or its checkbox cannot be safely updated.
    """
    repo = origin_repository()
    for number in numbers:
        target = [str(number), "--repo", f"github.com/{repo}"]
        data = gh_data(
            run_bytes(
                [find_tool(GH), "pr", "view", *target, "--json", "author,state,body"]
            )
        )
        if not isinstance(data, dict):
            msg = f"PR {number}: cannot read pull request on origin"
            raise SystemExit(msg)
        author: object = data.get("author")
        login: object = author.get("login") if isinstance(author, dict) else None
        if not renovate_author(login, number, repo):
            msg = f"PR {number}: author is not {RENOVATE_AUTHOR}"
            raise SystemExit(msg)
        if data.get("state") != "OPEN":
            msg = f"PR {number}: pull request is not open"
            raise SystemExit(msg)
        body: object = data.get("body")
        if not isinstance(body, str):
            msg = f"PR {number}: cannot read pull request body"
            raise SystemExit(msg)
        updated = rebase_body(number, body)
        if updated is None:
            print(f"PR {number}: the Renovate rebase checkbox is already ticked")
            continue
        status, _ = invoke_gh(
            [GH, "pr", "edit", *target, "--body-file", "-"], updated, quiet=True
        )
        if status:
            print(
                f"PR {number}: could not tick the Renovate rebase checkbox",
                file=sys.stderr,
            )
            return status
        print(f"PR {number}: ticked the Renovate rebase checkbox")
    return 0


def delete_pr_branch(number: int) -> int:
    """Delete a finished PR's unprotected head branch from origin only.

    Raises:
        SystemExit: The PR or origin is unsafe, or protection can't be read.
    """
    repo = origin_repository()
    branch = closed_pr_head(number, repo)
    ref = quote(branch, safe="")
    done = run(
        [
            find_tool(GH),
            "api",
            "--hostname",
            "github.com",
            f"repos/{repo}/branches/{ref}",
        ]
    )
    data = gh_data(done)
    if (
        not isinstance(data, dict)
        or data.get("name") != branch
        or data.get("protected") is not False
    ):
        message = f"branch {branch} is protected or unreadable"
        raise SystemExit(message)
    return invoke_gh(
        [
            GH,
            "api",
            "--hostname",
            "github.com",
            "--method",
            "DELETE",
            f"repos/{repo}/git/refs/heads/{ref}",
        ],
        "",
    )[0]


def create_pr(argv: list[str], body: str, root: str | None, *, dry_run: bool) -> int:
    """Refuse an existing open PR, including a race reported by gh create."""
    done = run([find_tool(GH), "pr", "view", "--json", "number,state,url"])
    data = gh_data(done)
    if isinstance(data, dict) and all(
        type(data.get(key)) is kind
        for key, kind in (("state", str), ("number", int), ("url", str))
    ):
        data = cast("dict[str, object]", data)  # pragma: no mutate
        if data["state"] == "OPEN":
            message = (
                f"error: PR {data['number']} ({data['url']}) is already open; "
                + EDIT_DRAFT.format(number=data["number"])
            )
            print(message, file=sys.stderr)
            return 1
    elif not (done.returncode and "no pull requests found" in done.stderr.lower()):
        output = (done.stderr or done.stdout).rstrip("\n")
        print(
            f"cannot check for an open PR: {output}",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        return invoke_gh(argv, body, dry_run=True)[0]
    argv[0] = find_tool(GH)
    done = run(argv, stdin=body)
    output = "\n".join(
        filter(None, (done.stdout.rstrip("\n"), done.stderr.rstrip("\n")))
    )
    if done.returncode or "already exists" in output.lower():
        print(output, file=sys.stderr)
        if "already exists" in output.lower():
            found = PR_URL.search(output)
            number = found[2] if found else "<n>"
            print(
                "gh pr create failed; " + EDIT_DRAFT.format(number=number),
                file=sys.stderr,
            )
        return 1
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    if root is None:
        return 0
    return record_created(root, done.stdout)


def record_created(root: str, stdout: str) -> int:
    """Add the PR that gh printed to the opener's ledger; the exit code.

    Once the PR exists, a ledger that can't be written only warns.
    """
    found = PR_URL.search(stdout)
    if found is None:
        print("gh printed no pull request URL; nothing was recorded", file=sys.stderr)
        return 1
    try:
        opened.record_opened(root, [f"{found[1]}#{found[2]}"])
    except (opened.LedgerError, OSError) as exc:
        message = f"warning: the PR is open, but the ledger was not updated: {exc}"
        print(message, file=sys.stderr)
    return 0


def gh_json(argv: Sequence[str]) -> list[dict[str, object]]:
    """A gh command's JSON list output.

    Raises:
        SystemExit: gh fails or prints something other than a JSON list.
    """
    done = run([find_tool(GH), *argv])
    data = gh_data(done)
    if not isinstance(data, list):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"gh {' '.join(argv[:2])} failed: {why}"
        raise SystemExit(message)
    return cast("list[dict[str, object]]", data)  # pragma: no mutate


def head_of(number: int) -> str:
    """PR N's head commit.

    Raises:
        SystemExit: gh can't read the pull request.
    """
    done = run([find_tool(GH), "pr", "view", str(number), "--json", "headRefOid"])
    data = gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read the head of PR {number}: {why}"
        raise SystemExit(message)
    return str(cast("dict[str, object]", data)["headRefOid"])  # pragma: no mutate


STUCK_AFTER = dt.timedelta(minutes=60)
CANCEL_POLLS = 12
CANCEL_PAUSE = 5.0


def utcnow() -> dt.datetime:
    """The current time in UTC."""
    return dt.datetime.now(dt.UTC)


def pause(seconds: float) -> None:
    """Sleep between polls of a cancelled run."""
    time.sleep(seconds)


def run_view(run_id: object, field: str) -> dict[str, object]:
    """A run's ``gh run view --json FIELD`` mapping.

    Raises:
        SystemExit: gh can't read the run.
    """
    done = run([find_tool(GH), "run", "view", str(run_id), "--json", field])
    data = gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read run {run_id}: {why}"
        raise SystemExit(message)
    return cast("dict[str, object]", data)  # pragma: no mutate


def stuck_job(run_id: object) -> bool:
    """Whether a job of the run has been in progress longer than ``STUCK_AFTER``.

    Raises:
        SystemExit: gh can't read the run.
    """
    jobs = cast(
        "list[dict[str, str]]", run_view(run_id, "jobs")["jobs"]
    )  # pragma: no mutate
    return any(
        job["status"] == "in_progress"
        and utcnow() - dt.datetime.fromisoformat(job["startedAt"]) > STUCK_AFTER
        for job in jobs
    )


def finished_as(run_id: object) -> str | None:
    """The run's conclusion once it completes within the bounded polls, else None."""
    for _ in range(CANCEL_POLLS):
        view = run_view(run_id, "status,conclusion")
        if view["status"] == "completed":
            return str(view["conclusion"])
        pause(CANCEL_PAUSE)
    return None


def run_numbers(run_id: object) -> list[object]:
    """The pull request numbers GitHub lists for the run."""
    return cast(
        "list[object]",
        gh_json(
            [
                "api",
                f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}",
                "--jq",
                "[.pull_requests[].number]",
            ]
        ),
    )  # pragma: no mutate


def sole_pull_request(run_id: object, number: int) -> bool:
    """Whether the run belongs to the pull request alone.

    A run that lists other pull requests too is theirs as much as ours, so
    cancelling it would hit them; the note names them.
    """
    numbers = run_numbers(run_id)
    if number in numbers and numbers != [number]:
        others = ", ".join(str(n) for n in numbers if n != number)
        print(f"run {run_id} also belongs to PR {others}; skipping", file=sys.stderr)
    return numbers == [number]


def cancel_and_rerun(run_id: str) -> int:
    """Cancel the run, wait until it completes as cancelled, then rerun it."""
    code = invoke_gh([GH, "run", "cancel", run_id], "")[0]
    if code:
        return code
    outcome = finished_as(run_id)
    if outcome is None:
        print(f"run {run_id} did not finish cancelling; no rerun", file=sys.stderr)
        return 1
    if outcome != "cancelled":
        print(f"run {run_id} completed as {outcome}; no rerun", file=sys.stderr)
        return 1
    return invoke_gh([GH, "run", "rerun", run_id], "")[0]


def rerun_stuck(number: int) -> int:
    """Cancel PR N's stuck current-head run, wait for the cancel, then rerun it.

    Only a ``pull_request`` run on the PR's head commit and head branch
    counts. A fork's PR is refused, because its branch name can collide.

    Raises:
        SystemExit: gh can't read the pull request or its runs.
    """
    done = run(
        [
            find_tool(GH),
            "pr",
            "view",
            str(number),
            "--json",
            "headRefOid,headRefName,isCrossRepository",
        ]
    )
    data = gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read PR {number}: {why}"
        raise SystemExit(message)
    meta = cast("dict[str, object]", data)  # pragma: no mutate
    if meta["isCrossRepository"]:
        print(f"PR {number} comes from a fork; rerun --stuck refuses", file=sys.stderr)
        return 1
    head = str(meta["headRefOid"])
    runs = gh_json(
        [
            "run",
            "list",
            "--commit",
            head,
            "--event",
            "pull_request",
            "--branch",
            str(meta["headRefName"]),
            "--limit",
            "20",
            "--json",
            "databaseId,status,headSha",
        ]
    )
    stuck = [
        item
        for item in runs
        if item["headSha"] == head
        and item["status"] == "in_progress"
        and sole_pull_request(item["databaseId"], number)
        and stuck_job(item["databaseId"])
    ]
    if not stuck:
        print(f"no stuck run for the current head of PR {number}", file=sys.stderr)
        return 1
    if head_of(number) != head:
        print(f"PR {number} moved to a new head; run again", file=sys.stderr)
        return 1
    return cancel_and_rerun(str(stuck[0]["databaseId"]))


def rerun(number: int) -> int:
    """Rerun the failed jobs of PR N's latest current-head run.

    Raises:
        SystemExit: gh can't read the pull request or its runs.
    """
    runs = gh_json(
        [
            "run",
            "list",
            "--commit",
            head_of(number),
            "--limit",
            "1",
            "--json",
            "databaseId,conclusion",
        ]
    )
    if not runs or runs[0]["conclusion"] != "failure":
        print(f"no failed run for the current head of PR {number}", file=sys.stderr)
        return 1
    return invoke_gh([GH, "run", "rerun", str(runs[0]["databaseId"]), "--failed"], "")[
        0
    ]


GREEN_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
UNFINISHED = frozenset({"QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "PENDING"})
PENDING_CODE = 3


def check_kind(job: object) -> tuple[dict[str, object], str, str]:
    """A rollup entry as a mapping, its kind (CheckRun or StatusContext) and name.

    Raises:
        SystemExit: the entry isn't a mapping, has no usable name or context, or
            names both without a ``__typename``.
    """
    if not isinstance(job, dict):
        message = f"a check of the rollup isn't an object: {job!r}"
        raise SystemExit(message)
    job = cast("dict[str, object]", job)  # pragma: no mutate
    kind = job.get("__typename")
    if kind is None and (job.get("name") is None) != (job.get("context") is None):
        kind = "CheckRun" if job.get("context") is None else "StatusContext"
    name = job.get("context" if kind == "StatusContext" else "name")
    if (
        kind not in ("CheckRun", "StatusContext")
        or not isinstance(name, str)
        or not name
    ):
        message = f"a check of the rollup has no usable name or context: {job}"
        raise SystemExit(message)
    return job, kind, name


def status_outcome(name: str, state: object) -> tuple[str, str, str]:
    """A commit status reads only its state, and only SUCCESS is green."""
    label = state if isinstance(state, str) and state else "(none)"
    if state == "SUCCESS":
        return name, label, "green"
    return name, label, "pending" if state == "PENDING" else "failed"


def check_outcome(job: object) -> tuple[str, str, str]:
    """A check's name, the label to print and its kind: green, pending or failed.

    A check run reads its conclusion only once its status is COMPLETED, and is
    pending only for an unfinished status. A commit status reads from its state.
    Anything outside the known values is a failure, printed as written.
    """
    job, kind, name = check_kind(job)
    if kind == "StatusContext":
        return status_outcome(name, job.get("state"))
    status = job.get("status")
    value = job.get("conclusion") if status == "COMPLETED" else status
    label = value if isinstance(value, str) and value else "(none)"
    if isinstance(status, str) and status in UNFINISHED:
        return name, "PENDING", "pending"
    green = status == "COMPLETED" and label in GREEN_CONCLUSIONS
    return name, label, "green" if green else "failed"


def checks(number: int) -> int:
    """Print a PR's head SHA and checks; exit 0 only when all are green.

    Exit 1 for a failed or unknown check or no checks, and 3 when the rest are
    still unfinished.

    Raises:
        SystemExit: gh can't read the pull request, or a check has no name.
    """
    argv = [
        find_tool(GH),
        "pr",
        "view",
        str(number),
        "--json",
        "headRefOid,statusCheckRollup",
    ]
    done = run(argv)
    data = gh_data(done)
    if not isinstance(data, dict):
        why = " ".join((done.stderr or done.stdout).split())
        message = f"cannot read checks of PR {number}: {why}"
        raise SystemExit(message)
    data = cast("dict[str, object]", data)  # pragma: no mutate
    print(data["headRefOid"])
    rollup: object = data["statusCheckRollup"] or []
    if not isinstance(rollup, list):
        message = f"the checks of PR {number} aren't a list: {rollup!r}"
        raise SystemExit(message)
    jobs = cast("list[object]", rollup)  # pragma: no mutate
    if not jobs:
        print(f"no checks on PR {number}", file=sys.stderr)
        return 1
    failed = pending = False
    for job in jobs:
        name, label, kind = check_outcome(job)
        print(f"{name}: {label}")
        pending = pending or kind == "pending"
        failed = failed or kind == "failed"
    if failed:
        return 1
    return PENDING_CODE if pending else 0


def body_roots(body: str) -> set[str]:
    """Every ``session=`` root hash on a body's LLM trailer lines."""
    found = (BACKFILL_KEYS.fullmatch(line.strip()) for line in body.splitlines())
    return {
        attr[1] for line in found if line for attr in SESSION_ATTR.finditer(line[1])
    }


def warn_at_limit(count: int, limit: int, what: str) -> None:
    """Warn on stderr that a gh list may have stopped at its ``--limit``."""
    if count == limit:
        message = (
            f"warning: {what} stopped at its limit of {limit}; some may be missing"
        )
        print(message, file=sys.stderr)


def backfill(owner: str) -> int:
    """Add every PR of OWNER's repositories to the ledgers its trailers name."""
    repos = gh_json(
        ["repo", "list", owner, "--limit", str(REPO_LIMIT), "--json", "nameWithOwner"]
    )
    warn_at_limit(len(repos), REPO_LIMIT, f"gh repo list {owner}")
    for repo in sorted(str(entry["nameWithOwner"]) for entry in repos):
        pulls = gh_json(
            ["pr", "list", "-R", repo, "--state", "all", "--limit", str(PR_LIMIT),
             "--json", "number,body"]
        )  # fmt: skip
        warn_at_limit(len(pulls), PR_LIMIT, f"gh pr list -R {repo}")
        by_root: dict[str, set[str]] = {}
        for pull in pulls:
            body = str(pull.get("body") or "")  # pragma: no mutate -- no roots
            for root in body_roots(body):
                by_root.setdefault(root, set()).add(f"{repo}#{pull['number']}")
        try:
            for root, refs in sorted(by_root.items()):
                opened.record_opened(root, refs)
        except opened.LedgerError as exc:
            raise SystemExit(str(exc)) from exc
        recorded = set().union(*by_root.values())
        print(f"{repo}: {len(recorded)} of {len(pulls)} pull requests recorded")
    return 0


def opened_by(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """The opener's root hash and ``Opened-by`` line, or (None, None).

    An opener whose value fails the trailer grammar, such as one without a
    model, gets a warning and neither.
    """
    ident = opened.opener(env)
    if ident is None:
        if agent_identity.detect(env) is not None:
            message = (
                f"warning: no {OPENED_KEY} line or ledger entry: "
                "cannot resolve the root session"
            )
            print(message, file=sys.stderr)
        return None, None
    value = agent_identity.format_value(ident.attrs())
    if why := agent_identity.check_value(value):
        message = f"warning: no {OPENED_KEY} line or ledger entry: {value}: {why}"
        print(message, file=sys.stderr)
        return None, None
    return ident.root, f"{OPENED_KEY}: {value}"


def check_size(args: argparse.Namespace, checked: Checked) -> None:
    """Count the branch's added lines against the draft's base and flag an excess.

    The base tracking ref is refreshed once, without tags, except in a dry run,
    which writes no refs. A failed refresh or a count that stays unavailable
    only warns, for ``pr create`` too, so a broken counter never blocks a valid
    PR. A dry run reports an excess as a warning and still previews.
    """
    branch = base_of(checked.meta)
    dry_run = getattr(
        args,
        "dry_run",
        False,  # pragma: no mutate -- None is also false.
    )
    base = f"refs/remotes/origin/{branch}"
    verify = ("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if args.command == "create" and not target_is_origin():
        # gh may open the PR against a repository whose base origin doesn't track.
        message = "size count unavailable: gh may target a repository other than origin"
        print(message, file=sys.stderr)
        return
    if not dry_run:
        fetch = (
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--refmap=",
            "origin",
            f"+refs/heads/{branch}:{base}",
        )
        if git(*fetch)[0] != 0:
            # A stale tracking ref would count the wrong range, so stop here.
            print(f"size count unavailable: cannot fetch {branch}", file=sys.stderr)
            return
    if git(*verify)[0] != 0:
        print(f"size count unavailable: base branch {base} not found", file=sys.stderr)
        return
    try:
        counts = size_gate.size_counts(
            base,
            "HEAD",
            scc=_TOOL_PATHS.get().get((SCC, False), SCC),
            git=_TOOL_PATHS.get().get((GIT, False), GIT),
        )
    except size_gate.SizeCountError as exc:
        print(f"size count unavailable: {exc}", file=sys.stderr)
        return
    if args.command == "check":
        print(f"{counts.code_lines} code lines, limit {counts.code_limit}")
        print(f"{counts.raw_lines} raw lines, limit {counts.raw_limit}")
    if counts.failed is not None:
        count, limit = (
            (counts.code_lines, counts.code_limit)
            if counts.failed == "code"
            else (counts.raw_lines, counts.raw_limit)
        )
        why = f"{count} lines, limit {limit}: split into stacked PRs"
        if dry_run:
            print(f"warning: {args.draft}:1: {why}", file=sys.stderr)
            return
        checked.problems.append((1, why))
        print(f"{args.draft}:1: {why}")


def command(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Resolve the tools for check and create before running the subcommand."""
    if args.command not in ("check", "create"):
        return _run_command(args, env)
    names = (COG, *(name for name, _, _ in LINTERS), GIT)
    unwrapped = (GITLEAKS,)
    if args.command == "create":
        names += (GITLEAKS, GH)
    paths = find_tools(names, unwrapped=unwrapped)
    # Not required: without scc the size count only warns.
    with contextlib.suppress(SystemExit):
        paths.update(find_tools((SCC,)))
    token = _TOOL_PATHS.set({(name, name in unwrapped): p for name, p in paths.items()})
    try:
        return _run_command(args, env)
    finally:
        _TOOL_PATHS.reset(token)


def _run_command(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Check the draft and carry out the requested action."""
    checked = read_checked(args.draft)
    if args.command in ("check", "create"):
        # One read of the default branch feeds the size count, the trailers, and
        # `gh --base`, so a remote change mid-run can't split them.
        checked.meta.setdefault("base", base_of(checked.meta))
        check_size(args, checked)
    if checked.problems:
        return 1
    if args.command == "check":
        print("OK: the draft conforms")
        return 0
    if args.command == "create":
        root, line = opened_by(env)
        body = description(checked, env, branch_trailers(base_of(checked.meta)), line)
        post_text(checked.title, body, base_visibility())
        argv = [GH, "pr", "create", "--title", checked.title, "--body-file", "-"]
        argv += gh_args(checked.meta)
        return create_pr(argv, body, root, dry_run=args.dry_run)
    line, url = live_pr(args.pr)
    target = target_of_pr(url)
    body = description(checked, env, pr_trailers(args.pr, target), line)
    post_text(checked.title, body, repo_visibility(target))
    argv = [
        GH,
        "pr",
        "edit",
        str(args.pr),
        "--title",
        checked.title,
        "--body-file",
        "-",
    ]
    labels = cast("list[str]", checked.meta.get("labels", []))  # pragma: no mutate
    for label in labels:
        argv += ["--add-label", label]
    return invoke_gh(argv, body)[0]


def touched_paths(harness: str, data: agent_identity.Json) -> list[str]:
    """Files a harness's PostToolUse payload says the tool call wrote."""
    inputs = agent_identity.tool_input_of(data)
    path: object
    if harness == CODEX:
        return agent_identity.patch_paths(inputs.get("command"))
    if harness == ANTIGRAVITY:
        tool, args = agent_identity.agy_tool(data)
        path = agent_identity.agy_edit_path(args) if tool in AGY_EDITS else None
    else:
        path = inputs.get("file_path")
    return [path] if isinstance(path, str) and path else []


def hook(harness: str) -> int:
    """PostToolUse entry point: check the draft when the tool call wrote it.

    Exit 2 with the problems on stderr is what each harness feeds back to the
    agent. Any other call exits 0 without running a linter. The hook fails open:
    a crash or a missing tool prints a warning and exits 0, because a broken
    hook must never stop the agent's work.
    """
    try:
        data = read_hook_stdin()
        cwd = hook_cwd(data) or Path.cwd()
        if not in_tbhb_dev(cwd):
            return 0
        # The checks read the worktree's own schema, sections and configs.
        os.chdir(root(cwd))
        target = (root() / DRAFT).resolve()
        if not any((cwd / p).resolve() == target for p in touched_paths(harness, data)):
            return 0
        problems = load_checked(target).problems
    except (Exception, SystemExit) as exc:  # noqa: BLE001  # fail open on any crash
        print(f"pull request hook skipped: {exc}", file=sys.stderr)
        return 0
    if not problems:
        return 0
    print(f"{DRAFT} has problems. Fix them and save the file again.", file=sys.stderr)
    for line, why in problems:
        print(f"{DRAFT}:{line}: {why}", file=sys.stderr)
    return HOOK_FEEDBACK


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog=prog, description="Check a PR draft and open the PR."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook").add_argument("harness", choices=[CLAUDE, CODEX, ANTIGRAVITY])
    sub.add_parser("backfill-opened").add_argument("owner")
    sub.add_parser("ready").add_argument("pr", type=int, nargs="+")
    sub.add_parser("rebase").add_argument("pr", type=int, nargs="+")
    again = sub.add_parser("rerun")
    again.add_argument("pr", type=int)
    again.add_argument("--stuck", action="store_true")
    sub.add_parser("delete-branch").add_argument("pr", type=int)
    sub.add_parser("checks").add_argument("pr", type=int)
    # `tbhbdev alpha pr close` runs in close.py before this parser sees it, so
    # this entry only lists the verb in --help.
    sub.add_parser("close", help="close N --reason TEXT: close a pull request")
    for name in ("check", "create", "edit"):
        one = sub.add_parser(name)
        if name == "edit":
            one.add_argument("draft", type=Path)
        else:
            one.add_argument("draft", nargs="?", type=Path, default=DRAFT)
        if name == "create":
            one.add_argument("--dry-run", action="store_true")
        if name == "edit":
            one.add_argument("--pr", type=int, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    prog: str | None = None,
) -> int:
    """Parse the command line and run it."""
    args = build_parser(prog).parse_args(argv)
    env = os.environ if env is None else env
    if args.command == "hook":
        return hook(args.harness)
    if args.command == "backfill-opened":
        return backfill(args.owner)
    if args.command == "rerun":
        return (rerun_stuck if args.stuck else rerun)(args.pr)
    by_pr = {
        "delete-branch": delete_pr_branch,
        "checks": checks,
        "ready": ready,
        "rebase": rebase,
    }
    if args.command in by_pr:
        return by_pr[args.command](args.pr)
    try:
        return command(args, env)
    except agent_identity.AttributionError:
        print(
            "tbhbdev alpha pr: Attribution couldn't be resolved. Retry the pull "
            "request command from the main session after the subagent's command "
            "finishes and its session record is available.",
            file=sys.stderr,
        )
        return 1
