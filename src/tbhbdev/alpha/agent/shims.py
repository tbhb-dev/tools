"""Build the Bazel shims that a worktree's commit hooks require."""

from __future__ import annotations

import re
import subprocess  # nosec B404
from pathlib import Path

from tbhbdev.alpha.agent import worktree
from tbhbdev.alpha.agent.model import Invalid, Profile

SHIM_SECONDS = 600
_HOOK_CONFIGS = (".pre-commit-config.yaml", ".pre-commit-config.yml", "prek.toml")


def _references(workspace: Path) -> dict[Path, set[str]]:
    references: dict[Path, set[str]] = {}
    for name in _HOOK_CONFIGS:
        config = workspace / name
        if not config.is_file():
            continue
        for match in re.finditer(
            r"((?:[\w.-]+/)*\.bazel_env/bin)(?:/([\w.-]+))?",
            config.read_text(errors="replace"),
        ):
            directory, shim = match.groups()
            names = references.setdefault(Path(directory), set())
            if shim:
                names.add(shim)
    return references


def needed(workspace: Path, profile: Profile) -> bool:
    return profile is Profile.BUILD and any(
        not (workspace / directory).exists() for directory in _references(workspace)
    )


def build(workspace: Path, tool: str | None) -> None:
    if tool is None:
        message = "hook shims: bazelisk or bazel is required"
        raise Invalid(message)
    for directory, names in _references(workspace).items():
        _build(workspace, tool, directory, names)


def _build(workspace: Path, tool: str, directory: Path, names: set[str]) -> None:
    package = directory.parent.parent.as_posix()
    package = "" if package == "." else package
    target = f"//{package}:bazel_env"
    command = f"{Path(tool).name} run {target}"
    try:
        result = subprocess.run(  # nosec B603
            [tool, "run", target],
            cwd=workspace,
            capture_output=True,
            timeout=SHIM_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        message = f"hook shims: {command} timed out after {SHIM_SECONDS} seconds"
        raise Invalid(message) from exc
    except OSError as exc:
        message = f"hook shims: could not start {command}"
        raise Invalid(message) from exc
    # The target can build the shims and then fail when direnv is unavailable.
    required = sorted(names) or [""]
    missing = [
        (directory / name).as_posix()
        for name in required
        if not (workspace / directory / name).exists()
    ]
    if missing:
        detail = worktree.safe_preparation_detail(
            result.stderr.decode(errors="replace")
        )
        message = (
            f"hook shims: {command} failed (rc={result.returncode}); "
            f"missing {', '.join(missing)}: {detail}"
        )
        raise Invalid(message)
