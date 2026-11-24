"""Build the Bazel shims that a worktree's commit hooks require."""

from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path

from tbhbdev.alpha.agent import worktree
from tbhbdev.alpha.agent.model import Invalid, Profile

SHIM_SECONDS = 600
_BIN = ".bazel_env/bin"
_HOOK_CONFIGS = (".pre-commit-config.yaml", ".pre-commit-config.yml", "prek.toml")


def needed(workspace: Path, profile: Profile) -> bool:
    if profile is not Profile.BUILD or (workspace / _BIN).exists():
        return False
    for name in _HOOK_CONFIGS:
        config = workspace / name
        if config.is_file() and _BIN.encode() in config.read_bytes():
            return True
    return False


def build(workspace: Path, tool: str | None) -> None:
    if tool is None:
        message = "hook shims: bazelisk or bazel is required"
        raise Invalid(message)
    command = f"{Path(tool).name} run //:bazel_env"
    try:
        result = subprocess.run(  # nosec B603
            [tool, "run", "//:bazel_env"],
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
    if not (workspace / _BIN / "cog").exists():
        detail = worktree.safe_preparation_detail(
            result.stderr.decode(errors="replace")
        )
        message = f"hook shims: {command} failed (rc={result.returncode}): {detail}"
        raise Invalid(message)
