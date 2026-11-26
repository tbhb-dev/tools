"""Check an imported tree against its source commit and path map."""

import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from tbhbdev.alpha.monorepo.pathmap import rewrite_bytes
from tbhbdev.alpha.monorepo.source import (
    EXECUTABLE,
    LINK,
    MonorepoError,
    contents,
    files,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tbhbdev.alpha.monorepo.pathmap import PathMap
    from tbhbdev.alpha.monorepo.source import Blob

# Prose that names an old repository is a dated record and stays as written.
PROSE = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
MODULE = re.compile(rb"^module\s+(\S+)", re.MULTILINE)
# Repositories in the design's "Where each repository goes" path map
# (wiki/designs/monorepo/C-layout-and-ownership.md), using the source repository
# names, not their destination folders. Standalone repos stay out.
OLD_REPOS = (
    rb"tbhb-dev/(?:agent-capacity|agent-config|agent-memories|ansible|"
    rb"bazel-r2-cache|dashboard|design|dotfiles|evals|event-gateway|github-metrics|"
    rb"go\.tbhb\.dev|ops|pm|renovate-config|research|rules|runner-smoke|sm|spikes|"
    rb"telemetry|tools|wiki|work)"
)
WORKFLOW = "workflow uses an old repository"
ADDRESS = "address of an old repository"
GO_REPOSITORY = "Go path from an old repository"
BAZEL_DEP = "Bazel dependency from an old repository"
# The module names published by repositories in OLD_REPOS. The rules
# repository also declares modules for its language and tool integrations.
OLD_BAZEL_MODULES = (
    rb"(?:agent_capacity_dev|bazel_r2_cache_dev|dashboard|event_gateway_dev|"
    rb"github_metrics|go_tbhb_dev|ops|renovate_config|tbhb_rules(?:_\w+)?|"
    rb"tbhb_dev_(?:agent_config|agent_memories|design|dotfiles|drafts|pm|research|"
    rb"sm|telemetry|tools))"
)


def old_repositories(repo: str) -> bytes:
    """Match a repository in the design's path map or the source repository."""
    return rb"(?:" + OLD_REPOS + b"|" + re.escape(repo.encode()) + rb")"


def rules(
    repo: str, modules: set[str], local_modules: set[str]
) -> dict[str, re.Pattern[bytes]]:
    """Match executable references to repositories the monorepo absorbs."""
    addresses = old_repositories(repo)
    repository = addresses + rb"(?![\w.-])"
    found = {
        WORKFLOW: rb"\buses:\s*"
        rb"(?:[>|][-+]?[ \t]*(?:#[^\r\n]*)?\r?\n\s*)?['\"]?" + repository,
        "Renovate preset from an old repository": rb"['\"](?:github|local)>"
        + repository,
        ADDRESS: rb"(?<![\w./-])(?:https://github\.com/|ssh://(?:git@)?github\.com/|"
        rb"git@github\.com:)" + addresses + rb"(?:\.git)?(?![\w.-]|/[\w./-])",
        BAZEL_DEP: rb"(?m)^[ \t]*bazel_dep\s*\([^)]*?\bname\s*=\s*['\"]"
        + OLD_BAZEL_MODULES
        + rb"['\"]",
    }
    local = b"|".join(re.escape(m.encode()) for m in sorted(local_modules - modules))
    exclude = rb"(?!(?:" + local + rb")(?![\w.-]))" if local else b""
    go_path = rb"(?<![\w./:-])" + exclude
    for module in sorted(modules):
        found[f"Go path {module}"] = (
            go_path + re.escape(module.encode()) + rb"(?![\w.-])"
        )
    found[GO_REPOSITORY] = go_path + rb"github\.com/" + repository
    return {rule: re.compile(pattern) for rule, pattern in found.items()}


def yaml_references(
    data: bytes, repo: str, address: re.Pattern[bytes]
) -> Iterator[tuple[int, str]]:
    """Find workflow calls and clone URLs outside YAML descriptions."""
    repository = re.compile(old_repositories(repo) + rb"(?![\w.-])")
    pending = list(
        cast("Iterator[Node]", yaml.compose_all(data, Loader=yaml.SafeLoader))
    )
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, SequenceNode):
            pending.extend(node.value)
        elif isinstance(node, MappingNode):
            for key, value in node.value:
                if isinstance(key, ScalarNode):
                    if key.value == "description" and isinstance(
                        value, (ScalarNode, SequenceNode)
                    ):
                        continue
                    if (
                        key.value == "uses"
                        and isinstance(value, ScalarNode)
                        and repository.match(value.value.strip().encode())
                    ):
                        yield key.start_mark.line + 1, WORKFLOW
                pending.append(value)
        else:
            start = node.start_mark
            # PyYAML retains the source buffer when loading bytes in memory.
            raw = cast("str", start.buffer)[start.index : node.end_mark.index].encode()
            for match in address.finditer(raw):
                yield start.line + raw.count(b"\n", 0, match.start()) + 1, ADDRESS


def references(
    dest: str, data: bytes, patterns: dict[str, re.Pattern[bytes]], repo: str
) -> Iterator[str]:
    """One finding for each line of a non-prose file that matches a rule."""
    path = PurePosixPath(dest)
    if path.suffix in PROSE:
        return
    found: dict[int, str] = {}
    is_yaml = path.suffix in {".yml", ".yaml"}
    if is_yaml:
        try:
            for line, rule in yaml_references(data, repo, patterns[ADDRESS]):
                if rule == WORKFLOW:
                    found[line] = rule
                else:
                    found.setdefault(line, rule)
        except yaml.YAMLError as exc:
            yield f"{dest}: invalid YAML: {exc}"
    for rule, pattern in patterns.items():
        if is_yaml and rule in {WORKFLOW, ADDRESS}:
            continue
        if rule == BAZEL_DEP and not (
            path.name == "MODULE.bazel" or path.name.endswith(".MODULE.bazel")
        ):
            continue
        if (
            rule == GO_REPOSITORY
            and path.suffix != ".go"
            and path.name not in {"go.mod", "go.sum", "go.work", "go.work.sum"}
        ):
            continue
        for match in pattern.finditer(data):
            number = data.count(b"\n", 0, match.start()) + 1
            found.setdefault(number, rule)
    for number, rule in sorted(found.items()):
        yield f"{dest}:{number}: {rule}"


def go_modules(clone: str, listing: dict[str, Blob]) -> set[str]:
    """The source repository's module path; nested modules aren't repositories."""
    mods = [src for src in listing if src == "go.mod"]
    return {
        match.decode()
        for data in contents(clone, [listing[src].oid for src in mods])
        for match in MODULE.findall(data)
    }


def tree_references(
    scanned: list[tuple[str, bytes]], repo: str, modules: set[str]
) -> Iterator[str]:
    """Scan faithful files after collecting all imported module declarations."""
    local_modules = {
        m.decode()
        for dest, data in scanned
        if PurePosixPath(dest).name == "go.mod"
        for m in MODULE.findall(data)
    }
    patterns = rules(repo, modules, local_modules)
    for dest, data in scanned:
        yield from references(dest, data, patterns, repo)


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
    problems.extend(tree_references(scanned, made.repo, go_modules(clone, listing)))
    return problems
