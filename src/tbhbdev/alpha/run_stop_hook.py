"""Keep a headless run working until it meets its profile's stop condition."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import TypedDict, cast

from tbhbdev.alpha.agent import runfiles
from tbhbdev.alpha.identity import as_str, parse_line, read_tail
from tbhbdev.alpha.repo import git, read_hook_stdin

ENV = "TBHBDEV_RUN_STOP"
MAX_BLOCKS = 3
VERDICT = re.compile(r"VERDICT:[ \t]+(?:GO|BLOCK|NO-GO)(?:[ \t]+[0-9a-f]{40})?[ \t]*")


class StopRun(TypedDict):
    """Stop context set by run-host for this exec only."""

    profile: str
    workspace: str
    reference: str
    start: str | None
    counter: str
    log: str


def final_output(harness: str, data: dict[str, object]) -> str:
    """Return only the latest assistant response from the Stop event."""
    if harness != "antigravity":
        return as_str(data.get("last_assistant_message")) or ""
    transcript = as_str(data.get("transcriptPath"))
    if transcript is None:
        return ""
    # Antigravity's compact transcript can truncate the end of the response,
    # where the verdict belongs.
    full = Path(transcript).with_name("transcript_full.jsonl")
    lines = read_tail(full, 2**20) or read_tail(Path(transcript), 2**20) or []
    for line in reversed(lines):
        entry = parse_line(line) or {}
        if entry.get("type") == "PLANNER_RESPONSE":
            return as_str(entry.get("content")) or ""
    return ""


def build_reason(run: StopRun) -> str | None:
    """Check the run checkout and the history of its branch."""
    workspace = Path(run["workspace"])
    status, tracked = git(
        "-c",
        "core.fsmonitor=false",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "--ignore-submodules=dirty",
        cwd=workspace,
    )
    if status:
        return "Restore the run worktree and branch so Git can check the build."
    if tracked:
        return "Check git status and commit the intended tracked changes."
    start = run["start"]
    if start is not None:
        _, commit = git(
            "--no-replace-objects",
            "rev-list",
            "-1",
            f"--ancestry-path={start}",
            f"{start}..{run['reference']}",
            "--",
            cwd=workspace,
        )
        if not commit:
            return "Finish the build and commit the result on the run branch."
    return None


def retry(run: StopRun, reason: str) -> bool:
    """Record at most three blocks, even with concurrent Stop callbacks."""
    count = Path(run["counter"])
    with runfiles.locked(count.with_suffix(".lock")):
        try:
            blocked = int(count.read_text())
        except FileNotFoundError:
            blocked = 0
        if blocked >= MAX_BLOCKS:
            runfiles.append_text(
                Path(run["log"]),
                f"run-stop-hook: allowing stop after {MAX_BLOCKS} blocks. {reason}\n",
            )
            return False
        runfiles.atomic(count, str(blocked + 1))
    return True


def main(argv: list[str], prog: str) -> int:
    """Respond to the harness using its Stop hook protocol."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("harness", choices=("claude", "codex", "antigravity"))
    harness = parser.parse_args(argv).harness
    context = os.environ.get(ENV)
    if not context:
        return 0
    data = read_hook_stdin()
    if harness == "antigravity" and data.get("terminationReason") not in (
        "NO_TOOL_CALL",
        "TERMINAL_STEP_TYPE",
    ):
        return 0
    run = cast("StopRun", json.loads(context))
    if run["profile"] == "build":
        reason = build_reason(run)
    elif any(
        VERDICT.fullmatch(line) for line in final_output(harness, data).splitlines()
    ):
        reason = None
    else:
        reason = (
            "Finish the review and include a VERDICT: GO, VERDICT: BLOCK or "
            "VERDICT: NO-GO line."
        )
    try:
        if reason and retry(run, reason):
            decision = "continue" if harness == "antigravity" else "block"
            print(json.dumps({"decision": decision, "reason": reason}))
    except OSError, ValueError:
        print(
            "run-stop-hook: allowing stop because retry state is unavailable",
            file=sys.stderr,
        )
    return 0
