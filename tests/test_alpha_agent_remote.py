"""Shared run file names and source independence."""

from __future__ import annotations

import re
from pathlib import Path

from tbhbdev.alpha.agent.decisions import runfiles_names as files


def test_file_names() -> None:
    assert files.DIRECTORY == "tbhbdev/agent-runs"
    assert files.INDEX == "index.jsonl"
    assert files.SUFFIXES == (
        ".request.json",
        ".prompt",
        ".schema.json",
        ".pgid",
        ".launched",
        ".phase",
        ".out",
        ".out.final",
        ".log",
        ".rc",
        ".keep",
        ".rc.pending",
    )


def test_source_and_fixtures_are_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "src/tbhbdev/alpha/agent"
    paths = [
        *source.rglob("*.py"),
        root / "tests/agent_example.py",
        root / "tests/agent_driver_example.py",
    ]
    forbidden = re.compile(r"peer-(?:agent|run)\.sh|run.book|PEER_")
    for path in paths:
        assert forbidden.search(path.read_text()) is None, str(path)
