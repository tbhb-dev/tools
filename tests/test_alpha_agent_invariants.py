"""Keep clone-side host git confined to preparation, with no later callers."""

from __future__ import annotations

import ast
from pathlib import Path
lazy from collections.abc import Iterator

import pytest


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted_name(node.value)}.{node.attr}"
    return ""


def owned_calls(
    node: ast.AST, owner: str | None = None
) -> Iterator[tuple[ast.Call, str | None]]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = node.name
        # Generated mutation functions retain their original lexical owner.
        if owner.startswith("x_") and "__mutmut_" in owner:
            owner = owner[2:].partition("__mutmut_")[0]
    if isinstance(node, ast.Call):
        yield node, owner
    for child in ast.iter_child_nodes(node):
        yield from owned_calls(child, owner)


def imported_git_names(tree: ast.AST, *, local: bool) -> tuple[set[str], set[str]]:
    git_names = {"git"} if local else set()
    clone_names = {"clone_run"} if local else set()
    modules = {"worktree"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name.endswith(".worktree")
            )
        if isinstance(node, ast.ImportFrom):
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "worktree"
            )
            if (node.module or "").rsplit(".", 1)[-1] == "worktree":
                git_names.update(
                    a.asname or a.name for a in node.names if a.name == "git"
                )
                clone_names.update(
                    a.asname or a.name for a in node.names if a.name == "clone_run"
                )
    git_names.update(f"{module}.git" for module in modules)
    clone_names.update(f"{module}.clone_run" for module in modules)
    return git_names, clone_names


def workspace_expression(node: ast.AST, aliases: set[str]) -> bool:
    return any(
        (isinstance(part, ast.Name) and part.id in aliases)
        or (isinstance(part, ast.Attribute) and part.attr in {"worktree", "workspace"})
        for part in ast.walk(node)
    )


def workspace_aliases(tree: ast.AST) -> set[str]:
    aliases = {"worktree", "workspace"}
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    previous: set[str] = set()
    while previous != aliases:
        previous = aliases.copy()
        for node in assignments:
            if workspace_expression(node.value, aliases):
                aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return aliases


def git_invariant_errors(source: str, *, local: bool) -> list[str]:
    tree = ast.parse(source)
    git_names, clone_names = imported_git_names(tree, local=local)
    aliases = workspace_aliases(tree)
    errors = []
    for call, owner in owned_calls(tree):
        name = dotted_name(call.func)
        if name in git_names and (
            not local
            or (
                call.args
                and workspace_expression(call.args[0], aliases)
                and owner not in {"clone_run", "create"}
            )
        ):
            errors.append(f"git in {owner} at line {call.lineno}")
        if name in clone_names and not (local and owner == "create"):
            errors.append(f"clone_run in {owner} at line {call.lineno}")
    return errors


@pytest.mark.parametrize(
    ("local", "source", "valid"),
    [
        (False, "worktree.git(path, 'status')", False),
        (False, "from tbhbdev.alpha.agent import worktree as w\nw.git(path)", False),
        (False, "from tbhbdev.alpha.agent.worktree import git as g\ng(path)", False),
        (False, "worktree.clone_run(mirror, workspace, url)", False),
        (True, "def finish(workspace):\n git(workspace, 'status')", False),
        (True, "def finish(run):\n git(run.worktree, 'status')", False),
        (True, "def finish(workspace):\n path = workspace\n git(path)", False),
        (True, "def finish(workspace):\n clone_run(mirror, workspace, url)", False),
        (
            True,
            "def create(worktree):\n clone_run(mirror, worktree, url)\n git(worktree)",
            True,
        ),
        (True, "def clone_run(workspace):\n git(workspace, 'config')", True),
        (True, "def fetch(mirror):\n git(mirror, 'fetch')", True),
    ],
)
def test_git_invariant_rejects_later_calls_and_import_aliases(
    *, local: bool, source: str, valid: bool
) -> None:
    assert bool(git_invariant_errors(source, local=local)) is not valid


def test_git_after_launch_invariant() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    owner = root / "tbhbdev/alpha/agent/worktree.py"
    violations = {
        str(path.relative_to(root)): errors
        for path in root.rglob("*.py")
        if (errors := git_invariant_errors(path.read_text(), local=path == owner))
    }
    assert not violations
