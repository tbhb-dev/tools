"""One documented command includes every opt-in native test module."""

import ast
import re
import shlex
from pathlib import Path


def live_module(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(mark, ast.Name) and mark.id == "live_only"
            for mark in node.decorator_list
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "skipif"
            and any(
                isinstance(part, ast.Constant) and part.value == "TBHBDEV_SANDBOX_LIVE"
                for part in ast.walk(node)
            )
        ):
            return True
    return False


def test_documented_command_runs_every_opt_in_live_module() -> None:
    root = Path(__file__).resolve().parents[1]
    live = {
        str(path.relative_to(root))
        for path in (root / "tests").glob("test_*.py")
        if live_module(path)
    }
    assert live
    command = re.search(
        r"(?m)^TBHBDEV_SANDBOX_LIVE=1 uv run pytest .+$",
        (root / "docs/reference/alpha/agent-configuration.md").read_text(),
    )
    assert command is not None
    arguments = shlex.split(command[0])
    assert {arg for arg in arguments if arg.startswith("tests/")} == live
    assert arguments[arguments.index("--basetemp") + 1].startswith(
        "$HOME/.local/state/"
    )


def test_docs_document_limits_and_twelve_parent_git_starts() -> None:
    docs = Path(__file__).resolve().parents[1] / "docs/reference/alpha"
    limits = (docs / "agent-isolation-limits.md").read_text()
    text = (docs / "agent-configuration.md").read_text() + limits
    for phrase in (
        "typed list",
        "Remove it when upgrading",
        "other runs' temp directories",
        "account's `gh` token",
        "Docker config",
        "private Codex auth file",
        "final answer file",
        "web_search",
        "H3",
        "credential decision",
        "mise/shims",
        "--no-track",
        "0.166.0",
        "0.164.0",
        "git maintenance run --auto",
    ):
        assert phrase in text
    counts = re.findall(r"`[^`]+` \((\d+)\)", limits)
    assert len(counts) == 7
    assert sum(map(int, counts)) == 12
