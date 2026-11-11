"""Host verdicts and the exact Git operations executed by the live harness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tbhbdev.alpha.agent.decisions.policy import GIT_CONFIG_PATHS

PROOF = "sandbox commit proof\n"
PROOF_FILE = "sandbox-commit-proof.txt"


def commit_verdict(
    before: str, after: str, message: str, *, present: bool, content: str
) -> str:
    generated = any(
        line.startswith("Generated-by: LLM ")
        and "harness=codex:" in line
        and "model=" in line
        for line in message.splitlines()
    )
    return (
        "committed"
        if before != after and present and content == PROOF and generated
        else "unproven"
    )


def git_child_verdict(code: int | None, *, unchanged: bool) -> str:
    if not unchanged:
        return "changed"
    if code is None:
        return "unproven"
    return "writable" if code == 0 else "blocked"


def child_snapshot(path: Path) -> dict[str, tuple[int, int, bytes | str]]:
    if not path.exists() and not path.is_symlink():
        return {}
    result = {}
    for entry in (path, *sorted(path.rglob("*"))):
        info = entry.lstat()
        value = (
            str(entry.readlink())
            if entry.is_symlink()
            else (b"" if entry.is_dir() else entry.read_bytes())
        )
        result[str(entry.relative_to(path))] = (info.st_mode, info.st_ino, value)
    return result


def probe(workspace: Path, witness: Path, profile: str) -> None:
    results = {}
    if profile == "build":
        (workspace / PROOF_FILE).write_text(PROOF)
        for operation, command in (
            ("add", ["git", "add", "--", PROOF_FILE]),
            ("commit", ["git", "commit", "-m", "test: sandbox commit proof"]),
        ):
            result = subprocess.run(
                command, cwd=workspace, capture_output=True, check=False, timeout=30
            )
            results[operation] = result.returncode
    for child in GIT_CONFIG_PATHS[1:]:
        target = workspace / child
        if target.is_dir():
            target /= "sandbox-write-proof"
        try:
            with target.open("ab") as stream:
                stream.write(b"\nsandbox write proof\n")
        except OSError as exc:
            results[child] = exc.errno or 1
        else:
            results[child] = 0
    witness.write_text(json.dumps(results))


if __name__ == "__main__":
    probe(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
