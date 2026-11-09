# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha guard-op`: require the configured account on every op call.

PreToolUse checks shell commands and proposed writes in every directory.
Claude Bash and Write/Edit/MultiEdit/NotebookEdit, Codex Bash/shell/apply_patch and
Antigravity run_command/edit tools use the guard-gh-pr denial contracts.
The installer configures PreToolUse matchers for these writes.

The account comes only from the user config's [op] account. OP_ACCOUNT is
insufficient by design: every invocation must name the --account flag.
Writes compare sets of normalized offenders before and after the edit.
Only shell, Markdown shell fences, Python command expressions, notebook code
and YAML command fields are scanned. Invalid Python or YAML is skipped.
Unreadable input or a crash before finding an op call allows silently; a
crash after finding one denies.

Known gaps (allowed shapes): shell text piped or redirected into a shell
(``echo '...' | sh``, ``sh <<< '...'``, ``bash < file``); remote-shell
heredocs (``ssh host bash <<EOF``); command substitution or variables building
the program name (``bash -c "$(echo op) ..."``, ``$OP``, ``op$IFS``);
``--account`` after ``--``; Python argv built from variables or f-strings;
``su -c``, ``runuser``, ``parallel``, ``strace``, ``unbuffer``, ``uv run``,
``npx``, script files run by path, aliases, ``python -c``, and ANSI-C quoting
such as ``$'op'``. This is a shell-word scanner, not an interpreter.
Prose and arbitrary string literals are deliberately outside its scope.
Codex's PreToolUse matcher for apply_patch is unverified against Codex's
documentation and gets checked live after install.
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
    flag_word,
    program_of,
    shell_script,
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
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user", "--group"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "env": {"-u", "-C", "-S"},
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a"},
    "exec": {"-a"},
    "watch": {"-n", "--interval"},
    "stdbuf": {"-i", "-o", "-e"},
    "setsid": set(),
    "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "doas": {"-u"},
    "flock": {"-w", "-E", "--wait", "--conflict-exit-code"},
}
YAML_COMMANDS = {
    "run",
    "shell",
    "command",
    "cmd",
    "ansible.builtin.shell",
    "ansible.builtin.command",
}
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
SHEBANG = re.compile(r"^#!.*\b(?:sh|bash|zsh)\b")

OLD_FIELDS = ("old_string", "TargetContent", "OldText")
NEW_FIELDS = ("new_string", "ReplacementContent", "NewText")
CONTENT_FIELDS = ("content", "CodeContent", "Content")
PATCH_FIELDS = ("patch", "input", "command")
EDIT_FIELDS = ("edits", "ReplacementChunks")
SHELL_FENCES = {"sh", "bash", "shell", "zsh", "console", "sh-session", "shellscript"}


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
        if word == "function":
            i += 2
            continue
        if word in KEYWORDS | {"--"} or ASSIGNMENT.match(word):
            i += 1
            continue
        special = special_prefix(words[i:])
        if special is not None:
            return prefix(special)
        if name not in WRAPPERS | PREFIX_VALUES.keys():
            break
        i += 1
        i = skip_options(words, i, PREFIX_VALUES.get(name, set()))
        if name in {"timeout", "flock"}:
            i += 1
    return words[i:]


def special_prefix(words: list[str]) -> list[str] | None:
    """Lookups do not run a program; env split strings supply shell words."""
    name = program_of(words)
    if name == "mise" and words[1:2] in (["x"], ["exec"]):
        return mise_prefix(words)
    if name == "command" and words[1:2] in (["-v"], ["-V"]):
        return []
    if name == "env" and "-S" in words[1:]:
        split = words.index("-S")
        return [
            "env",
            *words[1:split],
            *shlex.split(words[split + 1]),
            *words[split + 2 :],
        ]
    return None


def mise_prefix(words: list[str]) -> list[str] | None:
    """Only mise's explicit separator identifies its command."""
    try:
        return words[words.index("--") + 1 :]
    except ValueError:
        return None


def remote(words: list[str]) -> list[str]:
    """SSH words after its options and destination."""
    return words[
        skip_options(words, skip_options(words, 1, SSH_VALUES) + 1, SSH_VALUES) :
    ]


def skip_options(words: list[str], index: int, values: set[str]) -> int:
    """Skip flags, consuming a following value only for known options."""
    while index < len(words) and words[index].startswith("-"):
        if words[index] == "--":
            return index + 1
        option = flag_word(words[index], values)
        index += 2 if option is not None and option[1] is None else 1
    return index


@dataclass
class ShellScanner(Scanner):
    """Preserve executable shell/SSH heredocs without changing the shared scanner."""

    owners: list[list[str]] = field(default_factory=list)

    @override
    def unquoted(self, char: str) -> None:
        # Consume redirections before the shared scanner splits ampersands.
        match = re.match(
            r"(?:[0-9]*[<>](?!<)[>&]?|&>)(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+|\s+[^\s;&|]+)",
            self.text[self.pos :],
        )
        if not self.in_word and match:
            self.pos += len(match[0])
        else:
            super().unquoted(char)

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
            yield from direct_calls(words)


def direct_calls(words: list[str]) -> Iterator[list[str]]:
    """An op call, or shell text sent to SSH's remote shell."""
    if not words:
        return
    if program_of(words) == "find":
        yield from find_calls(words)
    elif program_of(words) == "op":
        yield words
    elif program_of(words) == "ssh":
        yield from shell_calls(" ".join(remote(words)))


def find_calls(words: list[str]) -> Iterator[list[str]]:
    """Read each find execution clause up to its terminator."""
    rest = iter(words)
    for word in rest:
        if word in {"-exec", "-execdir"}:
            command: list[str] = []
            for arg in rest:
                if arg in {";", "+"}:
                    break
                command.append(arg)
            yield from shell_calls(shlex.join(command))


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
    elif key in {"argv", "command", "ansible.builtin.command"} and isinstance(
        child, list
    ):
        call = argv_call(child)
        if call:
            yield call
    elif key in {"script", "before_script", "after_script"} and isinstance(child, list):
        scripts: list[object] = child
        for script in scripts:
            if isinstance(script, str):
                yield from shell_calls(script)
    else:
        yield from yaml_calls(child)


def markdown_calls(text: str) -> Iterator[list[str]]:
    """Read shell fences, and only prompt lines in console fences."""
    active: tuple[str, str | None] | None = None
    lines: list[str] = []
    for line in text.splitlines():
        match = FENCE.fullmatch(line)
        if match and active is None:
            active = (
                match[1],
                fence_language(match[2]),
            )
        elif active is not None and line.strip() == active[0]:
            yield from fence_calls(active[1], lines)
            active = None
            lines.clear()
        elif active is not None:
            if active[1] not in {"console", "sh-session"}:
                lines.append(line)
            elif line.startswith(("$ ", "# ")):
                lines.append(line[2:])
    if active is not None:
        yield from fence_calls(active[1], lines)


def fence_language(info: str) -> str | None:
    """The first info token names the fence language."""
    token = next(iter(info.split()), None)
    return token.strip("{}").removeprefix(".").lower() if token else None


def fence_calls(language: str | None, lines: list[str]) -> Iterator[list[str]]:
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
        if header.split()[:1] in (["%%bash"], ["%%sh"]) or header.split()[:2] in (
            ["%%script", "bash"],
            ["%%script", "sh"],
            ["%%script", "zsh"],
        ):
            yield from shell_calls(body)
        else:
            for line in source.splitlines():
                magic = re.match(r"^\s*(?:[A-Za-z_]\w*\s*=\s*)?!(.*)$", line)
                if magic:
                    yield from shell_calls(magic[1])
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
            for value in yaml.safe_load_all(text):
                yield from yaml_calls(value)
        except yaml.YAMLError:
            return


def read_file(path: Path) -> str:
    """New files have empty pre-write content."""
    try:
        return path.read_text(errors="surrogateescape")
    except OSError:
        return ""


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
    index = next((i for i, cell in enumerate(cells) if cell.get("id") == cell_id), None)
    if index is None:
        return before
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
        if "cell_type" in inputs:
            cells[index]["cell_type"] = inputs["cell_type"]
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
        tool = identity.agy_tool(data)[0]
        bad = (
            check.offenders(shell_calls(text))
            if tool in {"Bash", "shell", "run_command"}
            else set()
        )
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
