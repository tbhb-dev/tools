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


def test_readme_command_runs_every_opt_in_live_module() -> None:
    root = Path(__file__).resolve().parents[1]
    live = {
        str(path.relative_to(root))
        for path in (root / "tests").glob("test_*.py")
        if live_module(path)
    }
    assert live
    command = re.search(
        r"(?m)^TBHBDEV_SANDBOX_LIVE=1 uv run pytest .+$",
        (root / "README.md").read_text(),
    )
    assert command is not None
    arguments = shlex.split(command[0])
    assert {arg for arg in arguments if arg.startswith("tests/")} == live
    assert arguments[arguments.index("--basetemp") + 1].startswith(
        "$HOME/.local/state/"
    )
