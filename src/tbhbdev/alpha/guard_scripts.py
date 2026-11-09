# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-scripts`: keep scripts for the operator in one place.

A PreToolUse hook beside ``guard-gh-poll``, with the same payload handling
and output contracts (``guard_gh_pr.command_of`` and ``decision``) and the
same shell-word scanner. Every script an agent writes for the operator to run
lives in ``~/.local/libexec/tbhb/``, named ``YYYY-MM-DD-<owner>-<slug>.sh``. A
revision is a new file with a ``.vN`` suffix, because a handed-over script is
never edited. Run logs go in ``~/.local/state/tbhb/<same name>/``.

This is a PreToolUse guard, not a sandbox. It catches the common ways an
agent writes a file, so a script doesn't end up in a cache directory, a
drafts repository or the operator's home, and it misses the rest.

A target is a script when its path ends in ``.sh``, or its content starts
with ``#!``. Content is known from the Write tool's ``content``, the added
lines of an apply_patch Add File hunk, the first line of a heredoc in the
same command as a redirect or ``tee``, and, for ``cp``, ``mv``, ``install``
and ``ln``, the source: its name, or its first two bytes when it is a
readable regular file (never a device or a FIFO).

The writers read are:

    Bash, run_command   redirects ``>``, ``>>``, ``&>``, ``>|`` and ``N>``,
                        ``tee`` with every file operand, the destinations
                        of ``cp``, ``mv``, ``install`` and ``ln``
                        (``-t DIR``, ``--target-directory=DIR`` and a
                        trailing directory included), ``bash -c``,
                        ``eval`` and a heredoc fed to a shell
    Write, Edit, MultiEdit, NotebookEdit, write_file
                        the target path
    apply_patch         Add, Update and Move to paths

``~``, ``$HOME`` and ``${HOME}`` expand, a relative path resolves against the
payload's cwd, and ``..`` is normalized. Symlinks aren't resolved, and
``/private/tmp`` is read as ``/tmp``. A target decides in this order:

1. Under ``~/.local/libexec/tbhb/``: an existing file is refused, whatever
   writes it. A new file must sit directly in that directory and be named
   ``YYYY-MM-DD-<owner>-<slug>[.vN].sh`` with a real date. Otherwise allowed.
2. A script in a git work tree whose top directory, or whose main
   repository's directory for a linked work tree, has a name starting with
   ``drafts`` is refused. The ``.git`` directory or ``gitdir:`` file and its
   ``commondir`` decide; git never runs.
3. A script in any other git work tree is allowed.
4. A script under this session's scratchpad,
   ``/tmp/claude-<uid>/<project>/<session>/scratchpad/``, is allowed.
5. A script under a run-scoped ``$TMPDIR``, strictly below ``/tmp``, is
   allowed.
6. A script anywhere else under ``$HOME`` or ``/tmp`` is refused. Paths
   elsewhere are left alone.
7. A target that isn't a script is allowed.

Allowing prints nothing, and so does any crash or unreadable input, because a
broken hook must never stop the agent's work.

Known gaps: a command run after ``cd`` still resolves against the payload's
cwd, a heredoc that feeds a later command in a pipeline isn't tied to it,
content from ``echo`` or ``printf`` is unknown, an Edit that creates a file
counts only its name, a variable other than ``$HOME`` stays literal, and a
``.git`` directory in ``$HOME`` itself makes everything below it a work tree.
"""

import datetime as dt
import json
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, override

from tbhbdev.alpha import identity
from tbhbdev.alpha.guard_gh_poll import BodyScanner, inner_script
from tbhbdev.alpha.guard_gh_pr import (
    command_of,
    decision,
    flag_values,
    program_of,
    skip_prefix,
    split_args,
)
from tbhbdev.alpha.repo import hook_cwd, read_hook_stdin

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

type Json = dict[str, object]

PLACE = (
    "Write scripts for the operator to "
    "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh. A revision is a new "
    "file with a .vN suffix, because a handed-over script is never edited. "
    "Run logs go in ~/.local/state/tbhb/<same name>/."
)
NAME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+-[a-z0-9-]+(\.v[0-9]+)?\.sh")
DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})-.*")
SCRATCHPAD = re.compile(r"/tmp/claude-[0-9]+/[^/]+/[^/]+/scratchpad/.+")  # noqa: S108 # nosec B108 -- the rule names /tmp
PRIVATE_TMP = re.compile(r"^/private(?=/tmp(?:/|$))")
FD = re.compile(r"[0-9]+")
DUP = re.compile(r"[0-9]+-?|-")
COPIERS = {"cp", "mv", "install", "ln"}
# The flags of cp, mv, install and ln that take a value.
COPY_VALUE_FLAGS = {
    "-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner",
    "-g", "--group", "--strip-program",
}  # fmt: skip
TARGET_DIRECTORY = {"-t", "--target-directory"}
CONTENT_FIELDS = ("content", "CodeContent", "Content")
PATCH_FIELDS = ("patch", "input", "command")
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write_file"}


class Write(NamedTuple):
    """A file a tool call writes: its normalized path, and whether it is a script."""

    path: str
    script: bool


class Redirect(str):
    """A redirect operator in a command's words; the word after it is the target."""

    __slots__ = ()


class RedirectScanner(BodyScanner):
    """The heredoc-keeping scanner, reading ``&>``, ``>|`` and ``N>`` as redirects.

    The base scanner splits a command at ``&`` and ``|``, which would lose the
    target of ``&> f`` and ``>| f``.
    """

    @override
    def plain(self, char: str) -> None:
        """Read a redirect operator as one word, before the separator rules."""
        if char == ">" or (char == "&" and self.text.startswith(">", self.pos + 1)):
            self.redirect()
        else:
            super().plain(char)

    def redirect(self) -> None:
        """Read the operator and drop a file descriptor number before it."""
        end = self.text.index(">", self.pos) + 1
        if self.text[end : end + 1] in {">", "|", "&"}:
            end += 1
        descriptor = FD.fullmatch("".join(self.word)) is not None
        self.end_word()
        if descriptor:
            self.words.pop()
        self.words.append(Redirect(self.text[self.pos : end]))
        self.pos = end


def normalize(raw: str, base: str = "/") -> str:
    """An absolute path with ``..`` resolved and ``/private/tmp`` read as ``/tmp``."""
    return PRIVATE_TMP.sub("", posixpath.normpath(posixpath.join(base, raw)))


@dataclass(frozen=True)
class Place:
    """What paths resolve against: the cwd, the home directory and $TMPDIR."""

    cwd: str
    home: str
    tmpdir: str | None

    def resolve(self, raw: str) -> str:
        """The normalized absolute path of a word, with ``~`` and ``$HOME`` expanded."""
        for prefix in ("~", "$HOME", "${HOME}"):
            if raw == prefix or raw.startswith(f"{prefix}/"):
                raw = self.home + raw[len(prefix) :]
                break
        return normalize(raw, self.cwd)

    def write(self, raw: str, *, content: bool = False) -> Write:
        """A write to a word; a script when its content or its name says so."""
        path = self.resolve(raw)
        return Write(path, content or path.endswith(".sh"))

    def source_is_script(self, raw: str) -> bool:
        """Whether a copied file is named ``.sh`` or starts with ``#!``."""
        return raw.endswith(".sh") or has_shebang(self.resolve(raw))


def has_shebang(path: str) -> bool:
    """Whether a readable regular file starts with ``#!``, in two bytes read."""
    try:
        if not Path(path).is_file():
            return False
        with Path(path).open("rb") as file:
            return file.read(2) == b"#!"
    except OSError:
        return False


def first_str(fields: Json, keys: tuple[str, ...]) -> str:
    """The first of ``keys`` that holds text in ``fields``, or ""."""
    return next((v for key in keys if isinstance(v := fields.get(key), str)), "")


def tool_call(data: Json) -> tuple[object, Json]:
    """(tool name, input fields) from Claude, Codex or Antigravity hook stdin."""
    if isinstance(data.get("toolCall"), dict):
        name, inputs = identity.agy_tool(data)
        if isinstance(inputs, str):  # Antigravity may send the arguments as JSON text
            try:
                inputs = json.loads(inputs)
            except ValueError:
                return name, {}
    else:
        name, inputs = data.get("tool_name"), data.get("tool_input")
    return name, inputs if isinstance(inputs, dict) else {}


def redirects(command: list[str]) -> tuple[list[str], list[str]]:
    """(redirect targets, the other words) of a simple command.

    A target that duplicates a file descriptor (``>&2``) is not a file.
    """
    targets: list[str] = []
    words: list[str] = []
    rest = iter(command)
    for word in rest:
        if not isinstance(word, Redirect):
            words.append(word)
        elif (target := next(rest, None)) is not None and not (
            word.endswith("&") and DUP.fullmatch(target)
        ):
            targets.append(target)
    return targets, words


def copy_writes(program: str, args: list[str], place: Place) -> Iterator[Write]:
    """The files ``cp``, ``mv``, ``install`` or ``ln`` writes."""
    flags, operands = split_args(args, COPY_VALUE_FLAGS)
    if program == "install" and {"-d", "--directory"} & set(flags):
        return
    directory = flag_values(args, TARGET_DIRECTORY, COPY_VALUE_FLAGS)
    if directory:
        dest, sources = directory[-1], operands
    elif program == "ln" and len(operands) == 1:
        dest, sources = "./", operands
    elif len(operands) >= 2:  # noqa: PLR2004 -- a source and a destination
        dest, sources = operands[-1], operands[:-1]
    else:
        return
    into = (
        bool(directory)
        or len(sources) > 1
        or dest.endswith("/")
        or os.path.isdir(place.resolve(dest))  # noqa: PTH112 -- the string is the path
    )
    for source in sources:
        target = f"{dest}/{posixpath.basename(source)}" if into else dest
        yield place.write(target, content=place.source_is_script(source))


def command_writes(
    command: list[str], body: str | None, place: Place
) -> Iterator[Write]:
    """The writes of one simple command, and of the script it runs."""
    shebang = body is not None and body.startswith("#!")
    targets, words = redirects(command)
    for target in targets:
        yield place.write(target, content=shebang)
    rest = skip_prefix(words)
    if not rest:
        return
    program, args = program_of(rest), rest[1:]
    if program == "tee":
        for operand in (word for word in args if not word.startswith("-")):
            yield place.write(operand, content=shebang)
    elif program in COPIERS:
        yield from copy_writes(program, args, place)
    # ssh runs its command on another host, whose files this host can't judge.
    script = None if program == "ssh" else inner_script(rest, body)
    if script is not None:
        yield from bash_writes(script, place)


def bash_writes(text: str, place: Place) -> Iterator[Write]:
    """The writes in a shell command."""
    scanner = RedirectScanner(text)
    for index, command in enumerate(scanner.run()):
        yield from command_writes(command, scanner.bodies.get(index), place)


def patch_writes(patch: str, place: Place) -> list[Write]:
    """The paths an apply_patch patch adds, updates or moves to."""
    lines = patch.splitlines()
    writes: list[Write] = []
    for number, line in enumerate(lines):
        kind, _, name = line.partition(": ")
        if kind == "*** Add File":
            shebang = any(x.startswith("+#!") for x in lines[number + 1 : number + 2])
            writes.append(place.write(name, content=shebang))
        elif kind == "*** Update File":
            writes.append(place.write(name))
        elif kind == "*** Move to" and writes:
            writes.append(place.write(name, content=writes[-1].script))
    return writes


def file_writes(inputs: Json, place: Place) -> list[Write]:
    """The file a Write, Edit, MultiEdit, NotebookEdit or write_file call names."""
    raw = first_str(inputs, ("notebook_path",)) or identity.agy_edit_path(inputs)
    if not raw:
        return []
    content = first_str(inputs, CONTENT_FIELDS).startswith("#!")
    return [place.write(raw, content=content)]


def targets(data: Json, place: Place) -> list[Write]:
    """Every file a tool call writes."""
    tool, inputs = tool_call(data)
    if tool == "apply_patch":
        return patch_writes(first_str(inputs, PATCH_FIELDS), place)
    text = command_of(data)[0]
    if text:
        return list(bash_writes(text, place))
    return file_writes(inputs, place) if tool in FILE_TOOLS else []


def valid_name(name: str) -> bool:
    """Whether a file name is ``YYYY-MM-DD-<owner>-<slug>[.vN].sh`` with a real date."""
    if not NAME.fullmatch(name) or not (date := DATE.fullmatch(name)):
        return False
    try:
        dt.date(int(date[1]), int(date[2]), int(date[3]))
    except ValueError:
        return False
    return True


def main_repo_names(top: Path, dot_git: Path) -> list[str]:
    """The main repository's directory name, from a linked work tree's ``.git`` file."""
    try:
        gitdir = Path(top, dot_git.read_text().removeprefix("gitdir:").strip())
        common = (gitdir / "commondir").read_text().strip()
    except OSError, ValueError:
        return []
    path = Path(posixpath.normpath(posixpath.join(gitdir, common)))
    return [path.parent.name if path.name == ".git" else path.name]


def work_tree_names(path: str) -> list[str] | None:
    """The directory names that name the work tree around a path; None outside one."""
    for top in Path(path).parents:
        dot_git = top / ".git"
        if dot_git.is_dir():
            return [top.name]
        if dot_git.is_file():
            return [top.name, *main_repo_names(top, dot_git)]
    return None


def under(path: str, base: str) -> bool:
    """Whether ``path`` is ``base`` or below it."""
    return path == base or path.startswith(base.rstrip("/") + "/")


def libexec_refusal(path: str, libexec: str) -> str | None:
    """The deny message for a write under the libexec directory, or None."""
    if os.path.lexists(path):
        return (
            f"{path} already exists, and a handed-over script is never edited. "
            "Write the revision to a new file with a .vN suffix, such as "
            "YYYY-MM-DD-<owner>-<slug>.v2.sh."
        )
    if posixpath.dirname(path) != libexec or not valid_name(posixpath.basename(path)):
        return (
            f"{path} isn't a valid name for a script. Name it "
            "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh, with a real "
            "date and no subdirectory. A revision is a new file with a .vN suffix."
        )
    return None


def refusal(write: Write, place: Place) -> str | None:
    """The deny message for a write, or None when it is allowed."""
    path = write.path
    libexec = f"{place.home}/.local/libexec/tbhb"
    if under(path, libexec):
        return libexec_refusal(path, libexec)
    if not write.script:
        return None
    names = work_tree_names(path)
    if names is not None:
        drafts = any(name.startswith("drafts") for name in names)
        return (
            f"A drafts repository doesn't hold scripts: {path}. {PLACE}"
            if drafts
            else None
        )
    run_scoped = (
        place.tmpdir is not None
        and place.tmpdir.startswith("/tmp/")  # noqa: S108 # nosec B108 -- the rule names /tmp
        and under(path, place.tmpdir)
    )
    if SCRATCHPAD.fullmatch(path) or run_scoped:
        return None
    if under(path, place.home) or under(path, "/tmp"):  # noqa: S108 # nosec B108 -- the rule names /tmp
        return f"Scripts for the operator don't go in {path}. {PLACE}"
    return None


def first_refusal(data: Json, env: Mapping[str, str]) -> str | None:
    """The deny message for the first refused write of a tool call, or None."""
    tmpdir = env.get("TMPDIR")
    place = Place(
        cwd=str(hook_cwd(data) or Path.cwd()),
        home=normalize(env["HOME"]),
        tmpdir=tmpdir and normalize(tmpdir),
    )
    return next(
        (m for write in targets(data, place) if (m := refusal(write, place))), None
    )


def main() -> int:
    """Read the payload on stdin; print a deny decision for a refused write."""
    try:
        data = read_hook_stdin()
        message = first_refusal(data, os.environ)
        if message:
            print(json.dumps(decision(message, antigravity=command_of(data)[1])))
    except Exception:  # noqa: BLE001  # fail open on any crash
        return 0
    return 0
