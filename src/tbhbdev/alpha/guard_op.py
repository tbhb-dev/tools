# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-op`: require the configured account on every op call.

PreToolUse checks shell commands and proposed writes in every directory.
Claude Bash and Write/Edit/MultiEdit/NotebookEdit, Codex Bash/apply_patch and
Antigravity run_command/edit tools use the guard-gh-pr denial contracts.
All three harness configurations expose PreToolUse for these writes.

The account comes only from the user config's [op] account. OP_ACCOUNT is
insufficient by design: every invocation must name the --account flag.
Writes compare sets of normalized offenders before and after the edit.
Only shell, Markdown shell fences, Python command expressions, notebook code
and YAML command fields are scanned. Invalid Python or YAML is skipped.
Unreadable input or a crash before finding an op call allows silently; a
crash after finding one denies.

Known gaps: script files run by path (including files written with cat and
then run), variable or alias indirection, python -c, and other wrappers.
This is a shell-word scanner, not an interpreter. Prose and arbitrary string
literals are deliberately outside its scope.
"""

import ast
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, override

if TYPE_CHECKING:
    from collections.abc import Iterator
from pathlib import Path

import yaml

from tbhbdev import userconfig
from tbhbdev.alpha import identity
from tbhbdev.alpha.guard_gh_pr import (
    ASSIGNMENT,
    KEYWORDS,
    SHELLS,
    WRAPPERS,
    Scanner,
    command_of,
    decision,
    program_of,
    shell_script,
    simple_commands,
)
from tbhbdev.alpha.repo import hook_cwd, read_hook_stdin

SSH_VALUES = {
    "-o",
    "-i",
    "-p",
    "-l",
    "-J",
    "-F",
    "-E",
    "-b",
    "-c",
    "-D",
    "-L",
    "-R",
    "-S",
    "-W",
    "-w",
    "-m",
    "-O",
    "-Q",
    "-B",
}
PREFIX_VALUES = {
    "-u",
    "-g",
    "-n",
    "--user",
    "--group",
    "--adjustment",
    "-k",
    "-s",
    "--kill-after",
    "--signal",
}
YAML_COMMANDS = {
    "run",
    "shell",
    "command",
    "cmd",
    "ansible.builtin.shell",
    "ansible.builtin.command",
}
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(\w*)\s*$")
SHEBANG = re.compile(r"^#!.*\b(?:sh|bash|zsh)\b")

OLD_FIELDS = ("old_string", "TargetContent", "OldText")
NEW_FIELDS = ("new_string", "ReplacementContent", "NewText")
CONTENT_FIELDS = ("content", "CodeContent", "Content")
PATCH_FIELDS = ("patch", "input", "command")
EDIT_FIELDS = ("edits", "ReplacementChunks")
SHELL_FENCES = {"sh", "bash", "shell", "zsh", "console"}


class Cell(TypedDict, total=False):
    """The notebook code fields this hook inspects."""

    cell_type: str
    source: str | list[str]


class Notebook(TypedDict, total=False):
    """Notebook code cells, decoded from JSON."""

    cells: list[Cell]


def pick(data: dict[str, object], keys: tuple[str, ...]) -> object:
    """The first field present among the harnesses' spellings."""
    return next((data[key] for key in keys if key in data), None)


def prefix(words: list[str]) -> list[str]:
    """Extend the shared wrappers with their value options and launchers."""
    i = 0
    while i < len(words):
        word = words[i]
        name = word.rpartition("/")[2]
        if word in KEYWORDS or ASSIGNMENT.match(word):
            i += 1
            continue
        if name == "mise" and words[i + 1 : i + 2] in (["x"], ["exec"]):
            try:
                i = words.index("--", i + 2) + 1
            except ValueError:
                return words[i:]
            continue
        if name not in WRAPPERS | {"timeout", "nice"}:
            break
        i += 1
        i = skip_options(words, i, PREFIX_VALUES)
        if name == "timeout":
            i += 1
    return words[i:]


def remote(words: list[str]) -> list[str]:
    """SSH words after its options and destination."""
    return words[skip_options(words, 1, SSH_VALUES) + 1 :]


def skip_options(words: list[str], index: int, values: set[str]) -> int:
    """Skip flags, consuming a following value only for known options."""
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in values else 1
    return index


@dataclass
class ShellScanner(Scanner):
    """Preserve executable shell/SSH heredocs without changing the shared scanner."""

    owners: list[list[str]] = field(default_factory=list)

    @override
    def heredoc(self) -> None:
        super().heredoc()
        self.owners.append(self.words)

    @override
    def skip_heredoc_bodies(self) -> None:
        queued = self.heredocs
        owners = self.owners
        self.owners = []
        for (delim, strip), owner in zip(queued, owners, strict=True):
            command = prefix(owner)
            execute = shell_stdin(command)
            start = self.pos
            self.heredocs = [(delim, strip)]
            super().skip_heredoc_bodies()
            body = self.text[start : self.pos].splitlines(keepends=True)
            if body and body[-1].rstrip("\n").lstrip("\t" if strip else "") == delim:
                body.pop()
            if execute:
                self.commands.extend(ShellScanner("".join(body)).run())


def shell_stdin(command: list[str]) -> bool:
    """Whether a command executes its standard input as a shell script."""
    return bool(command) and (
        (program_of(command) in SHELLS and shell_script(command[1:]) is None)
        or (program_of(command) == "ssh" and not remote(command))
    )


def shell_calls(text: str) -> Iterator[list[str]]:
    """Split shell words while preserving heredocs through recursive scripts."""
    for command in ShellScanner(text.replace("\\\n", "")).run():
        words = prefix(command)
        if not words:
            continue
        program = program_of(words)
        if program in SHELLS:
            script = shell_script(words[1:])
            if script is not None:
                yield from shell_calls(script)
        elif program == "eval":
            yield from shell_calls(" ".join(words[1:]))
        else:
            for pre, simple in simple_commands(shlex.join(command)):
                yield from direct_calls(prefix(pre + simple))


def direct_calls(words: list[str]) -> Iterator[list[str]]:
    """An op call, or shell text sent to SSH's remote shell."""
    if program_of(words) == "op":
        yield words
    elif program_of(words) == "ssh":
        yield from shell_calls(" ".join(remote(words)))


def literal(value: ast.AST) -> object:
    """A string literal's value, or None for computed expressions."""
    return value.value if isinstance(value, ast.Constant) else None


class Argv(list[str]):
    """Literal argv: a flag may take a value computed by the generating code."""


def argv_call(words: list[object]) -> Argv | None:
    """Recognize literal op argv while retaining computed argument text."""
    if (
        not words
        or not isinstance(words[0], str)
        or words[0].rpartition("/")[2] != "op"
    ):
        return None
    return Argv(str(word) for word in words)


def argv_account(words: Argv) -> bool:
    """Generated argv needs the literal flag; runtime checks its value."""
    return any(
        word == "--account" or word.startswith("--account=") for word in words[1:]
    )


def python_calls(text: str) -> Iterator[list[str]]:
    """Inspect command argv literals and supported shell=True/system calls."""
    text = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(("%", "!"))
    )
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            call = argv_call([argv_word(e) for e in node.elts])
            if call:
                yield call
        script = python_shell(node)
        if script is not None:
            yield from shell_calls(script)


def argv_word(node: ast.AST) -> object:
    """Keep expression text in normalized argv for before/after comparison."""
    return literal(node) if isinstance(node, ast.Constant) else ast.unparse(node)


def python_shell(node: ast.AST) -> str | None:
    """A literal shell script in one of the supported Python call sites."""
    if (
        not isinstance(node, ast.Call)
        or not node.args
        or not isinstance(node.func, ast.Attribute)
    ):
        return None
    owner = node.func.value
    if not isinstance(owner, ast.Name):
        return None
    system = owner.id == "os" and node.func.attr in {"system", "popen"}
    subprocess = owner.id == "subprocess" and any(
        k.arg == "shell" and literal(k.value) is True for k in node.keywords
    )
    script = literal(node.args[0])
    return script if (system or subprocess) and isinstance(script, str) else None


def yaml_calls(value: object) -> Iterator[list[str]]:
    """Visit YAML command fields and argv lists, including nested workflows."""
    if isinstance(value, dict):
        fields: dict[object, object] = value
        for key, child in fields.items():
            yield from yaml_field(key, child)
    elif isinstance(value, list):
        children: list[object] = value
        for child in children:
            yield from yaml_calls(child)


def yaml_field(key: object, child: object) -> Iterator[list[str]]:
    """Interpret a YAML command field, or visit its children."""
    if key in YAML_COMMANDS and isinstance(child, str):
        yield from shell_calls(child)
    elif key == "argv" and isinstance(child, list):
        call = argv_call(child)
        if call:
            yield call
    else:
        yield from yaml_calls(child)


def markdown_calls(text: str) -> Iterator[list[str]]:
    """Read shell fences, and only prompt lines in console fences."""
    active: tuple[str, str] | None = None
    lines: list[str] = []
    for line in text.splitlines():
        match = FENCE.fullmatch(line)
        if match and active is None:
            active = (match[1], match[2])
        elif active is not None and line.strip() == active[0]:
            yield from fence_calls(active[1], lines)
            active = None
            lines.clear()
        elif active is not None:
            if active[1] != "console":
                lines.append(line)
            elif line.startswith(("$ ", "# ")):
                lines.append(line[2:])
    if active is not None:
        yield from fence_calls(active[1], lines)


def fence_calls(language: str, lines: list[str]) -> Iterator[list[str]]:
    """A supported shell fence, including an unclosed fence at end of file."""
    if language in SHELL_FENCES:
        yield from shell_calls("\n".join(lines))


def notebook_calls(text: str) -> Iterator[list[str]]:
    """Code cells support shell magics and Python command expressions."""
    book: Notebook = json.loads(text or "{}")
    for cell in book.get("cells", []):
        cell_type = cell.get("cell_type")
        if cell_type != "code":
            continue
        source = "".join(cell.get("source", []))
        header, _, body = source.partition("\n")
        if header.split()[:1] in (["%%bash"], ["%%sh"]):
            yield from shell_calls(body)
        else:
            for line in source.splitlines():
                if line.lstrip().startswith("!"):
                    yield from shell_calls(line.lstrip()[1:])
            yield from python_calls(source)


def file_calls(path: Path, text: str) -> Iterator[list[str]]:
    """Select the supported parser by filename or shell shebang."""
    name = path.name.removesuffix(".j2")
    if name.endswith((".sh", ".bash", ".zsh")) or SHEBANG.match(text):
        yield from shell_calls(text)
    elif path.suffix == ".md":
        yield from markdown_calls(text)
    elif path.suffix == ".py":
        yield from python_calls(text)
    elif path.suffix == ".ipynb":
        yield from notebook_calls(text)
    elif path.suffix in {".yml", ".yaml"}:
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError:
            return
        yield from yaml_calls(value)


def read_file(path: Path) -> str:
    """New files have empty pre-write content."""
    return path.read_text() if path.exists() else ""


@dataclass
class Patch:
    """One file's patch and the ordered hunk under construction."""

    path: Path
    before: str
    after: str
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    cursor: int = 0

    def flush(self) -> None:
        """Apply this hunk after the previous one."""
        target = "".join(self.old)
        replacement = "".join(self.new)
        start = (self.after + "\n").index(target, self.cursor)
        self.after = (
            self.after[:start] + replacement + self.after[start + len(target) :]
        )
        self.cursor = start + len(replacement)
        self.old = []
        self.new = []

    def line(self, line: str) -> None:
        """Collect hunk lines, flushing on a patch marker."""
        if line.startswith(("*** ", "@@")):
            self.flush()
        elif line.startswith(("+", "-", " ")):
            if not line.startswith("+"):
                self.old.append(line[1:] + "\n")
            if not line.startswith("-"):
                self.new.append(line[1:] + "\n")


def patch_files(patch: str, cwd: Path) -> Iterator[tuple[Path, str, str]]:
    """Apply ordered context hunks in memory, retaining unchanged file content."""
    current: Patch | None = None
    for line in patch.splitlines():
        if current is not None:
            current.line(line)
        if line.startswith(
            ("*** Add File: ", "*** Update File: ", "*** Delete File: ")
        ):
            if current is not None:
                yield current.path, current.before, current.after
            path = cwd / line.partition(": ")[2]
            before = read_file(path)
            current = Patch(path, before, before)
        elif current is not None and line.startswith("*** Move to: "):
            current.path = cwd / line.partition(": ")[2]
    if current is not None:
        current.flush()
        yield current.path, current.before, current.after


def replacements(text: str, edits: list[dict[str, object]]) -> str:
    """Apply the harness's ordered replacement operations."""
    for edit in edits:
        old = str(pick(edit, OLD_FIELDS))
        new = str(pick(edit, NEW_FIELDS))
        if edit.get("replace_all", edit.get("ReplaceAll")):
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
    return text


def writes(data: dict[str, object]) -> Iterator[tuple[Path, str, str]]:
    """Proposed file contents from Claude, Codex and Antigravity payloads."""
    cwd = hook_cwd(data) or Path.cwd()
    tool = data.get("tool_name")
    inputs = data.get("tool_input")
    if "toolCall" in data:
        tool, inputs = identity.agy_tool(data)
        if isinstance(inputs, str):
            inputs = json.loads(inputs)
    if not isinstance(inputs, dict):
        return
    fields: dict[str, object] = inputs
    if tool == "apply_patch":
        yield from patch_files(
            str(pick(fields, PATCH_FIELDS)),
            cwd,
        )
        return
    name = fields.get("notebook_path") or identity.agy_edit_path(fields)
    if not isinstance(name, str):
        return
    path = cwd / name
    before = read_file(path)
    content = pick(fields, CONTENT_FIELDS)
    if tool == "NotebookEdit":
        content = notebook_edit(before, fields)
    if not isinstance(content, str):
        edits = pick(fields, EDIT_FIELDS)
        if isinstance(edits, list):
            operations: list[dict[str, object]] = edits
        else:
            operations = [fields]
        content = replacements(before, operations)
    yield path, before, content


def notebook_edit(before: str, inputs: dict[str, object]) -> str:
    """Apply a proposed notebook cell insertion, replacement or deletion."""
    book: dict[str, list[dict[str, object]]] = json.loads(before)
    cell_id = inputs.get("cell_id")
    cells = book["cells"]
    index = next((i for i, cell in enumerate(cells) if cell.get("id") == cell_id), 0)
    mode = inputs.get("edit_mode")
    if mode == "delete":
        del cells[index]
    elif mode == "insert":
        cells.insert(
            index,
            {
                "cell_type": inputs.get("cell_type", "code"),
                "source": inputs["new_source"],
            },
        )
    else:
        cells[index]["source"] = inputs["new_source"]
    return json.dumps(book)


@dataclass
class Check:
    """Remember finds before any config read or other fallible operation."""

    found: str = ""
    account: str = ""
    antigravity: bool = False

    def offenders(self, calls: Iterator[list[str]]) -> set[str]:
        bad: set[str] = set()
        for words in calls:
            self.found = " ".join(words)
            self.account = userconfig.string("op", "account")
            valid = (
                argv_account(words)
                if isinstance(words, Argv)
                else valid_account(words, self.account)
            )
            if not valid:
                bad.add(self.found)
        return bad

    def message(self, command: str) -> str:
        if not self.account:
            return f"`{command}` is blocked. Set [op] account in the user config."
        return (
            f"`{command}` is blocked. Use the configured account {self.account}: "
            f"add --account {self.account}."
        )


def valid_account(words: list[str], account: str) -> bool:
    """Require at least one flag, with every value equal to the configured one."""
    values = [
        word.partition("=")[2] for word in words[1:] if word.startswith("--account=")
    ]
    for i, word in enumerate(words):
        if word == "--account":
            if i + 1 == len(words):
                return False
            values.append(words[i + 1])
    return bool(values) and all(value == account for value in values)


def main() -> int:
    """Read a PreToolUse payload and deny new offending invocations."""
    check = Check()
    try:
        data = read_hook_stdin()
        text, check.antigravity = command_of(data)
        bad = check.offenders(shell_calls(text))
        for path, before, after in writes(data):
            old = check.offenders(file_calls(path, before))
            bad |= check.offenders(file_calls(path, after)) - old
        if not bad:
            return 0
        message = check.message(min(bad))
    except Exception:  # noqa: BLE001 -- allow only before finding an op call
        if not check.found:
            return 0
        message = check.message(check.found)
    print(json.dumps(decision(message, antigravity=check.antigravity)))
    return 0
