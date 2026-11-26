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
    from collections.abc import Iterator

    from tbhbdev.alpha.monorepo.pathmap import Allow, PathMap
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
type Match = tuple[int, str, bytes]


def old_repositories(repo: str) -> bytes:
    """Match a repository in the design's path map or the source repository."""
    return rb"(?:tbhb-dev/" + OLD_REPO_NAMES + b"|" + re.escape(repo.encode()) + rb")"


def rules(
    repo: str, modules: set[str], local_modules: set[str]
) -> dict[str, re.Pattern[bytes]]:
    """Match executable references to repositories the monorepo absorbs."""
    addresses = old_repositories(repo)
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
    found[GO_REPOSITORY] = go_path + (
        rb"(?:github\.com/"
        + addresses
        + rb"|go\.tbhb\.dev/"
        + OLD_REPO_NAMES
        + rb")"
        + GO_SUFFIX
    )
    return {rule: re.compile(pattern) for rule, pattern in found.items()}


def pattern_references(
    data: bytes, patterns: dict[str, re.Pattern[bytes]], start_line: int = 1
) -> Iterator[Match]:
    for rule, pattern in patterns.items():
        for match in pattern.finditer(data):
            target = match["target"] if pattern.groups else match[0]
            yield start_line + data.count(b"\n", 0, match.start()), rule, target


def yaml_mapping_references(
    node: MappingNode,
    pending: list[Node],
    repository: re.Pattern[bytes],
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
                yield key.start_mark.line + 1, WORKFLOW, target


def yaml_scalar_references(
    token: ScalarToken, patterns: dict[str, re.Pattern[bytes]]
) -> Iterator[Match]:
    start = token.start_mark
    raw = cast("str", start.buffer)[start.index : token.end_mark.index]
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    line = start.line + 1
    if token.style in {"|", ">"}:
        # The block header can carry a YAML comment beside the style marker.
        _, _, raw = raw.partition("\n")
        line += 1
    yield from pattern_references(raw.encode(), patterns, line)


def yaml_references(
    data: bytes, repo: str, patterns: dict[str, re.Pattern[bytes]], *, galaxy: bool
) -> Iterator[Match]:
    """Scan YAML scalar values, with workflow calls identified by their key."""
    repository = re.compile(old_repositories(repo) + rb"(?![\w.-])")
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
                node, pending, repository, galaxy=galaxy and id(node) in roots
            )
            continue
        if token := scalars.get(node.end_mark.index):
            yield from yaml_scalar_references(token, patterns)


def bazel_arguments(call: ast.Call) -> dict[str | None, str]:
    return {
        keyword.arg: keyword.value.value
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
    deps: list[tuple[int, str]] = []
    for kind, call in bazel_calls(data):
        if kind == "local_path_override":
            args = bazel_arguments(call)
            if (name := args.get("module_name")) and inside_tree(
                tree, folder, args.get("path", "")
            ):
                local.add(name)
        elif kind == "bazel_dep":
            name = bazel_arguments(call).get("name", "")
            if OLD_BAZEL_MODULES.fullmatch(name):
                deps.append((call.lineno, name))
    for line, name in deps:
        if name not in local:
            yield line, BAZEL_DEP, name.encode()


def references(
    dest: str,
    data: bytes,
    patterns: dict[str, re.Pattern[bytes]],
    repo: str,
    tree: Path,
) -> Iterator[Match]:
    path = PurePosixPath(dest)
    if path.suffix in PROSE:
        return
    is_yaml = path.suffix in {".yml", ".yaml"}
    scoped = patterns.copy()
    if path.suffix != ".go" and path.name not in GO_FILES:
        scoped.pop(GO_REPOSITORY)
    if is_yaml:
        scoped.pop(WORKFLOW)
        try:
            yield from yaml_references(
                data, repo, scoped, galaxy=path.name == "galaxy.yml"
            )
        except yaml.YAMLError as exc:
            yield 0, f"invalid YAML: {exc}", b""
        return
    yield from pattern_references(scan_text(path, data), scoped)
    if path.name == "MODULE.bazel" or path.name.endswith(".MODULE.bazel"):
        yield from bazel_references(data, tree / path.parent, tree)


def go_modules(clone: str, listing: dict[str, Blob]) -> set[str]:
    """The source repository's module path; nested modules aren't repositories."""
    mods = [src for src in listing if src == "go.mod"]
    return {
        match.decode()
        for data in contents(clone, [listing[src].oid for src in mods])
        for match in MODULE.findall(scan_text(PurePosixPath("go.mod"), data))
    }


def file_findings(
    dest: str, matches: Iterator[Match], allow: list[tuple[int, Allow]], used: set[int]
) -> Iterator[str]:
    found: dict[int, str] = {}
    for line, rule, match in matches:
        if not line:
            yield f"{dest}: {rule}"
            continue
        target = match.decode(errors="replace")
        if matched := {i for i, entry in allow if entry.pattern.search(target)}:
            used.update(matched)
        elif rule == WORKFLOW:
            found[line] = rule
        else:
            found.setdefault(line, rule)
    for line, rule in sorted(found.items()):
        yield f"{dest}:{line}: {rule}"


def tree_references(
    scanned: list[tuple[str, bytes]],
    repo: str,
    modules: set[str],
    tree: Path,
    allow: list[Allow],
) -> Iterator[str]:
    """Scan faithful files and report any exception that suppressed no finding."""
    local_modules = {
        m.decode()
        for dest, data in scanned
        if (path := PurePosixPath(dest)).name == "go.mod"
        for m in MODULE.findall(scan_text(path, data))
    }
    patterns = rules(repo, modules, local_modules)
    used: set[int] = set()
    root = tree.resolve()
    for dest, data in scanned:
        inside = PurePosixPath(dest)
        entries = [(i, a) for i, a in enumerate(allow, 1) if inside.full_match(a.path)]
        yield from file_findings(
            dest, references(dest, data, patterns, repo, root), entries, used
        )
    for i, entry in enumerate(allow, 1):
        if i not in used:
            yield (
                f"allow entry {i} ({entry.path}): matches no findings ({entry.reason})"
            )


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
            scanned, made.repo, go_modules(clone, listing), tree, made.allow
        )
    )
    return problems
