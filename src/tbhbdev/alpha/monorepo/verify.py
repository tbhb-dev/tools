"""Check an imported tree against its source commit and path map."""

import ast
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import ScalarToken

from tbhbdev.alpha.monorepo.pathmap import rewrite_bytes
from tbhbdev.alpha.monorepo.reference_text import GO_FILES, METADATA, scan_text
from tbhbdev.alpha.monorepo.source import (
    EXECUTABLE,
    LINK,
    MonorepoError,
    contents,
    files,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from tbhbdev.alpha.monorepo.pathmap import PathMap
    from tbhbdev.alpha.monorepo.source import Blob

# Prose that names an old repository is a dated record and stays as written.
PROSE = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
MODULE = re.compile(rb"^module\s+(\S+)", re.MULTILINE)
# Repositories in the design's "Where each repository goes" path map
# (wiki/designs/monorepo/C-layout-and-ownership.md), using the source repository
# names, not their destination folders. Standalone repos stay out.
OLD_REPO_NAMES = (
    rb"(?:agent-capacity|agent-config|agent-memories|ansible|"
    rb"bazel-r2-cache|dashboard|design|dotfiles|evals|event-gateway|github-metrics|"
    rb"go\.tbhb\.dev|ops|pm|renovate-config|research|rules|runner-smoke|sm|spikes|"
    rb"telemetry|tools|wiki|work)"
)
WORKFLOW = "workflow uses an old repository"
ADDRESS = "address of an old repository"
GO_REPOSITORY = "Go path from an old repository"
BAZEL_DEP = "Bazel dependency from an old repository"
# The module names published by repositories in OLD_REPO_NAMES. The rules
# repository also declares modules for its language and tool integrations.
OLD_BAZEL_MODULES = re.compile(
    r"(?:agent_capacity_dev|bazel_r2_cache_dev|dashboard|event_gateway_dev|"
    r"github_metrics|go_tbhb_dev|ops|renovate_config|tbhb_rules(?:_\w+)?|"
    r"tbhb_dev_(?:agent_config|agent_memories|design|dotfiles|drafts|evals|pm|"
    r"research|sm|spikes|telemetry|tools))"
)
PRESET = "Renovate preset from an old repository"
REFERENCE_END = rb"[^\s'\"\x60,\]\)}]*"
GO_SUFFIX = rb"(?![\w.-])(?:/[\w./-]+)?"
# cspell:words renovaterc
ROOT_RENOVATE = (
    ".renovaterc",
    ".renovaterc.json",
    ".renovaterc.json5",
    "renovate.json",
    "renovate.json5",
)
YAML_LINE_BREAK = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")
YAML_SUFFIXES = frozenset({".yml", ".yaml"})
# The statement and its reference can start on different lines.
type Match = tuple[int, str, int]


def old_repositories(repo: str) -> bytes:
    """Match a repository in the design's path map or the source repository."""
    return rb"(?:tbhb-dev/" + OLD_REPO_NAMES + b"|" + re.escape(repo.encode()) + rb")"


def rules(
    addresses: bytes, modules: set[str], local_modules: set[str], *, source_only: bool
) -> dict[str, re.Pattern[bytes]]:
    """Match executable references to repositories the monorepo absorbs."""
    repository = addresses + rb"(?![\w.-])"
    found = {
        WORKFLOW: rb"\buses:\s*"
        rb"(?:[>|][-+]?[ \t]*(?:#[^\r\n]*)?\r?\n\s*)?['\"]?(?P<target>"
        + repository
        + REFERENCE_END
        + rb")",
        PRESET: rb"['\"](?P<target>(?:github|local)>"
        + repository
        + REFERENCE_END
        + rb")",
        ADDRESS: rb"(?<![\w./-])(?:https://github\.com/|ssh://(?:git@)?github\.com/|"
        rb"git@github\.com:)" + addresses + rb"(?:\.git)?(?![\w.-]|/[\w./-])",
    }
    local = b"|".join(re.escape(m.encode()) for m in sorted(local_modules - modules))
    exclude = rb"(?!(?:" + local + rb")(?![\w.-]))" if local else b""
    go_path = rb"(?<![\w./:-])" + exclude
    for module in sorted(modules):
        found[f"Go path {module}"] = go_path + re.escape(module.encode()) + GO_SUFFIX
    go_repos = rb"github\.com/" + addresses
    if not source_only:
        go_repos += rb"|go\.tbhb\.dev/" + OLD_REPO_NAMES
    found[GO_REPOSITORY] = go_path + rb"(?:" + go_repos + rb")" + GO_SUFFIX
    return {rule: re.compile(pattern) for rule, pattern in found.items()}


def pattern_references(
    data: bytes, patterns: dict[str, re.Pattern[bytes]], start_line: int = 1
) -> Iterator[Match]:
    for rule, pattern in patterns.items():
        for match in pattern.finditer(data):
            line = start_line + data.count(b"\n", 0, match.start())
            target_start = match.start("target") if pattern.groups else match.start()
            yield line, rule, line + data.count(b"\n", match.start(), target_start)


def yaml_mapping_references(
    node: MappingNode,
    pending: list[Node],
    repository: re.Pattern[bytes],
    scalars: dict[int, ScalarToken],
    *,
    galaxy: bool,
) -> Iterator[Match]:
    for key, value in node.value:
        if not isinstance(key, ScalarNode):
            pending.append(value)
            continue
        if galaxy and key.value in METADATA:
            continue
        if key.value == "description" and isinstance(value, (ScalarNode, SequenceNode)):
            continue
        pending.append(value)
        if key.value == "uses" and isinstance(value, ScalarNode):
            target = value.value.strip().encode()
            if repository.match(target):
                line, raw = yaml_scalar_text(scalars[value.end_mark.index])
                raw = raw.removeprefix('"').removeprefix("'")
                target_line = line + raw.count("\n", 0, len(raw) - len(raw.lstrip()))
                yield key.start_mark.line + 1, WORKFLOW, target_line


def yaml_scalar_text(token: ScalarToken) -> tuple[int, str]:
    start = token.start_mark
    raw = cast("str", start.buffer)[start.index : token.end_mark.index]
    raw = YAML_LINE_BREAK.sub("\n", raw)
    line = start.line + 1
    if token.style in {"|", ">"}:
        # The block header can carry a YAML comment beside the style marker.
        _, _, raw = raw.partition("\n")
        line += 1
    return line, raw


def yaml_references(
    data: bytes,
    addresses: bytes,
    patterns: dict[str, re.Pattern[bytes]],
    *,
    galaxy: bool,
) -> Iterator[Match]:
    """Scan YAML scalar values, with workflow calls identified by their key."""
    repository = re.compile(addresses + rb"(?![\w.-])")
    pending = list(
        cast("Iterator[Node]", yaml.compose_all(data, Loader=yaml.SafeLoader))
    )
    # Scalar tokens exclude anchors and tags, which can precede YAML comments.
    scalars = {
        token.end_mark.index: token
        for token in cast("Iterator[object]", yaml.scan(data, Loader=yaml.SafeLoader))
        if isinstance(token, ScalarToken)
    }
    roots = {id(node) for node in pending}
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, SequenceNode):
            pending.extend(node.value)
            continue
        if isinstance(node, MappingNode):
            yield from yaml_mapping_references(
                node, pending, repository, scalars, galaxy=galaxy and id(node) in roots
            )
            continue
        if token := scalars.get(node.end_mark.index):
            line, raw = yaml_scalar_text(token)
            yield from pattern_references(raw.encode(), patterns, line)


def bazel_arguments(call: ast.Call) -> dict[str | None, tuple[str, int]]:
    return {
        keyword.arg: (keyword.value.value, keyword.value.lineno)
        for keyword in call.keywords
        if isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    }


def inside_tree(tree: Path, folder: Path, path: str) -> bool:
    try:
        return bool(path) and (folder / path).resolve().is_relative_to(tree)
    except OSError, RuntimeError, ValueError:
        return False


def bazel_calls(data: bytes) -> Iterator[tuple[str, ast.Call]]:
    try:
        statements = ast.parse(data).body
    except SyntaxError:
        return
    for statement in statements:
        match statement:
            case ast.Expr(value=ast.Call(func=ast.Name(id=name)) as call):
                yield name, call
            case _:
                continue


def bazel_references(data: bytes, folder: Path, tree: Path) -> Iterator[Match]:
    """Find old Bazel dependencies without a local override in this module."""
    local: set[str] = set()
    deps: list[tuple[int, str, int]] = []
    for kind, call in bazel_calls(data):
        if kind == "local_path_override":
            args = bazel_arguments(call)
            name, _ = args.get("module_name", ("", 0))
            path, _ = args.get("path", ("", 0))
            if name and inside_tree(tree, folder, path):
                local.add(name)
        elif kind == "bazel_dep":
            name, target_line = bazel_arguments(call).get("name", ("", 0))
            if OLD_BAZEL_MODULES.fullmatch(name):
                deps.append((call.lineno, name, target_line))
    for line, name, target_line in deps:
        if name not in local:
            yield line, BAZEL_DEP, target_line


def references(
    dest: str,
    data: bytes,
    patterns: dict[str, re.Pattern[bytes]],
    addresses: bytes,
    bazel_root: Path | None,
) -> Iterator[Match]:
    path = PurePosixPath(dest)
    if path.suffix in PROSE:
        return
    scoped = patterns.copy()
    if path.suffix != ".go" and path.name not in GO_FILES:
        scoped.pop(GO_REPOSITORY)
    if path.suffix in YAML_SUFFIXES:
        scoped.pop(WORKFLOW)
        try:
            yield from yaml_references(
                data, addresses, scoped, galaxy=path.name == "galaxy.yml"
            )
        except yaml.YAMLError as exc:
            yield 0, f"invalid YAML: {exc}", 0
        return
    yield from pattern_references(scan_text(path, data), scoped)
    if bazel_root is not None and (
        path.name == "MODULE.bazel" or path.name.endswith(".MODULE.bazel")
    ):
        yield from bazel_references(data, bazel_root / path.parent, bazel_root)


def go_modules(clone: str, listing: dict[str, Blob]) -> set[str]:
    """The source repository's module path; nested modules aren't repositories."""
    mods = [src for src in listing if src == "go.mod"]
    return {
        match.decode()
        for data in contents(clone, [listing[src].oid for src in mods])
        for match in MODULE.findall(scan_text(PurePosixPath("go.mod"), data))
    }


def file_findings(
    dest: str,
    data: bytes,
    matches: Iterator[Match],
    allow: list[str],
    used: set[tuple[str, str]],
) -> Iterator[str]:
    allowed = set(allow)
    lines: list[str] = []
    if allowed:
        content = data.decode(errors="surrogateescape")
        lines = (
            YAML_LINE_BREAK.split(content)
            if PurePosixPath(dest).suffix in YAML_SUFFIXES
            else content.split("\n")
        )
    found: dict[int, str] = {}
    for line, rule, target_line in matches:
        if not line:
            yield f"{dest}: {rule}"
            continue
        if allowed and (text := lines[target_line - 1].strip()) in allowed:
            used.add((dest, text))
        elif rule == WORKFLOW:
            found[line] = rule
        else:
            found.setdefault(line, rule)
    for line, rule in sorted(found.items()):
        yield f"{dest}:{line}: {rule}"


def tree_references(
    scanned: list[tuple[str, bytes]],
    made: PathMap,
    modules: set[str],
    tree: Path,
    *,
    shared: Iterable[tuple[str, bytes]] = (),
) -> Iterator[str]:
    """Scan faithful files and report any exception that suppressed no finding."""
    local_modules = {
        m.decode()
        for dest, data in scanned
        if (path := PurePosixPath(dest)).name == "go.mod"
        for m in MODULE.findall(scan_text(path, data))
    }
    used: set[tuple[str, str]] = set()
    root = tree.resolve()
    for source_only, files_to_check in ((False, scanned), (True, shared)):
        if source_only:
            addresses = re.escape(made.repo.encode())
            patterns = rules(addresses, set(), set(), source_only=True)
            bazel_root = None
        else:
            addresses = old_repositories(made.repo)
            patterns = rules(addresses, modules, local_modules, source_only=False)
            bazel_root = root
        for dest, data in files_to_check:
            yield from file_findings(
                dest,
                data,
                references(dest, data, patterns, addresses, bazel_root),
                made.allow.get(dest, []),
                used,
            )
    for dest, lines in made.allow.items():
        for line in lines:
            if (dest, line) not in used:
                yield f"{dest}: allow entry matches no findings: {line!r}"


def read(path: Path) -> tuple[str, bytes] | None:
    """The git mode and bytes of the link or file at ``path``, if it is one.

    Raises:
        MonorepoError: The path can't be inspected or read.
    """
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            # Path.readlink normalizes the target, losing byte differences.
            return LINK, os.fsencode(os.readlink(path))  # noqa: PTH115
        if stat.S_ISREG(mode):
            git_mode = EXECUTABLE if mode & stat.S_IXUSR else "100644"
            return git_mode, path.read_bytes()
    except OSError as exc:
        message = f"{path}: cannot read: {exc}"
        raise MonorepoError(message) from exc
    return None


def inventory(tree: Path, dest: str, problems: list[str]) -> set[str]:
    """Files and links in the imported area, without following directory links.

    Append scan and stat errors to ``problems`` and continue with other paths.

    Raises:
        MonorepoError: A component leading to the imported area is a link.
        OSError: A component leading to the imported area can't be inspected.
    """
    folder = (tree / dest).absolute()
    for parent in (*reversed(folder.parents), folder):
        if stat.S_ISLNK(parent.lstat().st_mode):
            message = f"{parent}: symbolic link in destination path"
            raise MonorepoError(message)
    if stat.S_ISREG(folder.lstat().st_mode):
        return {dest}
    return walk(tree, folder, problems)


def walk(tree: Path, folder: Path, problems: list[str]) -> set[str]:
    found: set[str] = set()
    pending = [folder]
    while pending:
        folder = pending.pop()
        try:
            children = list(folder.iterdir())
        except OSError as exc:
            problems.append(f"{folder}: cannot scan: {exc}")
            continue
        for child in children:
            try:
                mode = child.lstat().st_mode
            except OSError as exc:
                problems.append(f"{child}: cannot scan: {exc}")
                continue
            if stat.S_ISDIR(mode):
                pending.append(child)
                continue
            found.add(child.relative_to(tree.absolute()).as_posix())
    return found


def shared_files(
    tree: Path, imported: set[str], problems: list[str]
) -> Iterator[tuple[str, bytes]]:
    """Read the shared GitHub and Renovate files without following symlinks."""
    root = tree.absolute()
    if any(path.is_symlink() for path in (*root.parents, root)):
        return
    paths = {
        dest for dest in ROOT_RENOVATE if (tree / dest).exists(follow_symlinks=False)
    }
    github = tree / ".github"
    if github.is_dir(follow_symlinks=False):
        paths.update(walk(tree, github.absolute(), problems))
    for dest in sorted(paths - imported):
        try:
            got = read(tree / dest)
        except MonorepoError as exc:
            problems.append(str(exc))
            continue
        if got is not None and got[0] != LINK:
            yield dest, got[1]


def check(clone: str, commit: str, made: PathMap, tree: Path) -> list[str]:
    """Every problem found, one line each; none means the import is faithful."""
    listing = files(clone, commit)
    problems = made.gaps(listing)
    writes = made.writes()
    try:
        actual = inventory(tree, made.dest, problems)
    except MonorepoError as exc:
        problems.append(str(exc))
        actual = set()
    except OSError as exc:
        problems.append(f"{tree / made.dest}: cannot scan: {exc}")
        actual = set()
    srcs = sorted(listing.keys() & writes.keys())
    expected = {writes[src][0] for src in srcs}
    problems.extend(
        f"{dest}: not produced by the map" for dest in sorted(actual - expected)
    )
    scanned: list[tuple[str, bytes]] = []
    for src, data in zip(
        srcs, contents(clone, [listing[s].oid for s in srcs]), strict=True
    ):
        dest, entry = writes[src]
        mode = listing[src].mode
        try:
            wanted = rewrite_bytes(src, data, entry)
            got = read(tree / dest) if dest in actual else None
        except MonorepoError as exc:
            problems.append(str(exc))
            continue
        if got is None:
            problems.append(f"{dest}: missing from the tree")
        elif got[0] != mode:
            problems.append(f"{dest}: mode {got[0]}, source has {mode}")
        elif got[1] != wanted:
            problems.append(f"{dest}: differs from {src}")
        elif mode != LINK:
            scanned.append((dest, wanted))
    problems.extend(
        tree_references(
            scanned,
            made,
            go_modules(clone, listing),
            tree,
            shared=shared_files(tree, actual, problems),
        )
    )
    return problems
