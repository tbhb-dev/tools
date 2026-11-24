"""Launch several review runs, wait for all of them, and print one block per run.

A review session that polls each run turn by turn spends most of its context on
status checks. This command launches the runs through ``agent exec``, waits on
each through ``agent wait`` in parallel, and returns once, so the session wakes
once per request.
"""

import argparse
import json
import re
import subprocess  # nosec B404
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, cast

LAUNCHER = [sys.executable, "-I", "-m", "tbhbdev", "alpha", "agent"]
MIN_RUNS = 2
MAX_RUNS = 4
HEAD = re.compile(r"[0-9a-fA-F]{40}")
VERDICT = re.compile(r"^[\s>*_#-]*((?:VERDICT|RECOMMEND)\b.*)$", re.MULTILINE)
FINDING = re.compile(r"^[\s>*_-]*(P[123])\b", re.MULTILINE)
RC = re.compile(r"^\S+: rc=(\S+)")
# Spec key -> `agent exec` flag. Every run is a review-profile run.
REQUIRED = ("name", "prompt_file", "host", "repo", "model", "effort", "head")
FLAGS = {
    "host": "--host",
    "repo": "--repo",
    "model": "--model",
    "effort": "--effort",
    "harness": "--harness",
    "account": "--account",
    "claude_profile": "--claude-profile",
}
OPTIONAL = ("harness", "account", "claude_profile", "fetch_refs")


class SpecError(ValueError):
    """The spec file is not a valid pair of runs."""


class Result(NamedTuple):
    """What one run came to, ready to print."""

    name: str
    head: str
    state: str
    verdict: str
    counts: str


def load_spec(path: Path) -> list[dict[str, object]]:
    """Read and validate the spec: 2 to 4 runs with a closed set of keys."""
    try:
        data: object = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as exc:
        message = f"cannot read {path}: {exc}"
        raise SpecError(message) from exc
    top = cast("dict[str, object]", data) if isinstance(data, dict) else {}
    runs = top.get("runs")
    if set(top) != {"runs"} or not isinstance(runs, list):
        message = f'the spec is {{"runs": [...]}} with {MIN_RUNS} to {MAX_RUNS} runs'
        raise SpecError(message)
    items = cast("list[object]", runs)
    if not MIN_RUNS <= len(items) <= MAX_RUNS:
        message = f"the spec needs {MIN_RUNS} to {MAX_RUNS} runs"
        raise SpecError(message)
    checked = [check_run(item) for item in items]
    names = [str(run["name"]) for run in checked]
    if len(set(names)) != len(names):
        message = "run names must differ"
        raise SpecError(message)
    return checked


def check_run(item: object) -> dict[str, object]:
    """Refuse a run with a missing, unknown or malformed key."""
    if not isinstance(item, dict):
        message = "each run is an object"
        raise SpecError(message)
    run = cast("dict[str, object]", item)
    unknown = sorted(set(run) - {*REQUIRED, *OPTIONAL})
    missing = [key for key in REQUIRED if key not in run]
    if unknown or missing:
        message = f"run keys: unknown {unknown}, missing {missing}"
        raise SpecError(message)
    for key, value in run.items():
        if key != "fetch_refs" and not (isinstance(value, str) and value):
            message = f"run {run['name']!r}: {key} is a non-empty string"
            raise SpecError(message)
    refs = run.get("fetch_refs", [])
    if not isinstance(refs, list) or not all(
        isinstance(ref, str) for ref in cast("list[object]", refs)
    ):
        message = f"run {run['name']!r}: fetch_refs is a list of strings"
        raise SpecError(message)
    if not HEAD.fullmatch(str(run["head"])):
        message = f"run {run['name']!r}: head is a 40-digit commit sha"
        raise SpecError(message)
    return run


def exec_argv(run: dict[str, object]) -> list[str]:
    """The `agent exec` command line for one run."""
    argv = [*LAUNCHER, "exec", "--profile", "review"]
    for key, flag in FLAGS.items():
        if key in run:
            argv += [flag, str(run[key])]
    for ref in cast("list[str]", run.get("fetch_refs", [])):
        argv += ["--fetch-ref", ref]
    return [*argv, str(run["name"]), str(run["prompt_file"])]


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one sibling command and capture its output."""
    return subprocess.run(  # nosec B603
        argv, capture_output=True, text=True, check=False
    )


def tail(text: str) -> str:
    """The last line of a command's output, for a one-line state."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def summarize(answer: str) -> tuple[str, str]:
    """The verdict line and the P1/P2/P3 counts of a run's answer."""
    found = VERDICT.search(answer)
    verdict = found[1].strip() if found else "unparsed"
    tags = FINDING.findall(answer)
    counts = " ".join(f"{tag}={tags.count(tag)}" for tag in ("P1", "P2", "P3"))
    return verdict, counts


def run_one(run: dict[str, object]) -> tuple[Result, bool]:
    """Launch one run, wait for it, and say whether it ended with rc=0."""
    name, head = str(run["name"]), str(run["head"])
    launched = run_command(exec_argv(run))
    if launched.returncode != 0:
        state = f"not launched ({tail(launched.stdout + launched.stderr)})"
        return Result(name, head, state, "none", "none"), False
    waited = run_command([*LAUNCHER, "wait", name])
    first, _, answer = waited.stdout.partition("\n")
    code = RC.match(first)
    rc = code[1] if code else f"unknown ({tail(waited.stdout + waited.stderr)})"
    verdict, counts = summarize(answer)
    return Result(name, head, f"rc={rc}", verdict, counts), rc == "0"


def render(result: Result) -> str:
    """One block per run."""
    return (
        f"{result.name}\n"
        f"  head: {result.head}\n"
        f"  state: {result.state}\n"
        f"  verdict: {result.verdict}\n"
        f"  findings: {result.counts}\n"
        f"  log: tbhbdev alpha agent log {result.name}\n"
    )


def main(argv: list[str], prog: str) -> int:
    """Launch the spec's runs, wait for all, print the blocks."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("spec", type=Path, help='JSON file: {"runs": [...]}')
    args = parser.parse_args(argv)
    try:
        runs = load_spec(args.spec)
    except SpecError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        outcomes = list(pool.map(run_one, runs))
    print("\n".join(render(result) for result, _ in outcomes), end="")
    return 0 if all(ended for _, ended in outcomes) else 1
