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

from tbhbdev.alpha import pull_request as pr

LAUNCHER = [sys.executable, "-I", "-m", "tbhbdev", "alpha", "agent"]
MIN_RUNS = 2
MAX_RUNS = 3
HEAD = re.compile(r"[0-9a-fA-F]{40}")
VERDICT = re.compile(r"^[\s>*_#-]*((?:VERDICT|RECOMMEND)\b.*)$", re.MULTILINE)
FINDING = re.compile(r"^[\s>*_-]*(P[123])\b", re.MULTILINE)
REVIEW_VERDICT = re.compile(
    r"^##[ \t]+Verdict[ \t]*\r?\n\s*(GO|HOLD)[ \t]*\r?$", re.MULTILINE
)
REVIEW_FINDING = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?(BLOCK|TECH-DEBT|SECURITY-DEBT)[ \t]+", re.MULTILINE
)
RC = re.compile(r"^\S+: rc=(\S+)")
# Spec key -> `agent exec` flag. Every run is a review-profile run.
REQUIRED = ("name", "prompt_file", "host", "repo", "model", "effort", "head", "pr")
FLAGS = {
    "host": "--host",
    "repo": "--repo",
    "model": "--model",
    "effort": "--effort",
    "harness": "--harness",
    "account": "--account",
    "claude_profile": "--claude-profile",
    "reason": "--reason",
}
OPTIONAL = ("harness", "account", "claude_profile", "reason", "fetch_refs")
LAUNCH_UNKNOWN = "75"  # the launcher kept the run record: the run may be live


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
    """Read and validate the spec: 2 or 3 runs of one head by different harnesses."""
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
    for key in ("repo", "head", "pr"):
        if len({str(run[key]).lower() for run in checked}) != 1:
            message = f"every run reviews the same {key}"
            raise SpecError(message)
    harnesses = [str(run.get("harness", "codex")) for run in checked]
    if len(set(harnesses)) != len(harnesses):
        message = "each run needs its own harness, so providers stay independent"
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
        if key not in ("fetch_refs", "pr") and not (isinstance(value, str) and value):
            message = f"run {run['name']!r}: {key} is a non-empty string"
            raise SpecError(message)
    check_launch_keys(run)
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


def check_launch_keys(run: dict[str, object]) -> None:
    """Validate the pull request number and harness launch options."""
    number = run["pr"]
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        message = f"run {run['name']!r}: pr is a positive integer"
        raise SpecError(message)
    harness = run.get("harness", "codex")
    if harness not in ("codex", "claude", "agy"):
        message = f"run {run['name']!r}: harness must be codex, claude or agy"
        raise SpecError(message)
    if harness == "claude" and not (run.get("reason") and run.get("claude_profile")):
        message = f"run {run['name']!r}: a claude run needs reason and claude_profile"
        raise SpecError(message)


def exec_argv(run: dict[str, object]) -> list[str]:
    """The `agent exec` command line for one run."""
    argv = [*LAUNCHER, "exec", "--profile", "review", "--ref", str(run["head"])]
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


def pull_request_state(repo: str, number: int) -> tuple[str, bool, str]:
    """Read the PR's head, draft flag and state in one call; refuse an unreadable PR."""
    done = pr.run(
        [
            pr.find_tool(pr.GH),
            "pr",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            "headRefOid,isDraft,state",
        ]
    )
    data = pr.gh_data(done)
    if not isinstance(data, dict):
        message = f"cannot read pull request {number}"
        raise SpecError(message)
    info = cast("dict[str, object]", data)
    return (
        str(info.get("headRefOid")),
        info.get("isDraft") is not False,
        str(info.get("state")),
    )


def preflight(run: dict[str, object]) -> None:
    """Refuse a draft, a closed PR, or a head that differs from the spec."""
    number = cast("int", run["pr"])
    head, draft, state = pull_request_state(str(run["repo"]), number)
    if draft or state != "OPEN" or head.lower() != str(run["head"]).lower():
        message = (
            f"pull request {number} must be open, ready, and at the spec head "
            f"(now {head}, state {state}, draft {draft})"
        )
        raise SpecError(message)


def summarize(answer: str) -> tuple[str, str]:
    """The verdict and finding counts of a run's answer."""
    found = REVIEW_VERDICT.search(answer) or VERDICT.search(answer)
    verdict = found[1].strip() if found else "unparsed"
    tags = FINDING.findall(answer) + REVIEW_FINDING.findall(answer)
    counts = " ".join(
        f"{tag}={tags.count(tag)}"
        for tag in ("P1", "P2", "P3", "BLOCK", "TECH-DEBT", "SECURITY-DEBT")
    )
    return verdict, counts


def run_one(run: dict[str, object]) -> tuple[Result, bool]:
    """Launch one run, wait for it, and say whether it ended with rc=0."""
    name, head = str(run["name"]), str(run["head"])
    launched = run_command(exec_argv(run))
    # A run can finish before exec acknowledges the launch. Wait reads its answer.
    if launched.returncode not in (
        0,
        int(LAUNCH_UNKNOWN),
    ) and not launched.stdout.startswith(f"{name}: rc="):
        state = f"not launched (exit {launched.returncode})"
        return Result(name, head, state, "none", "none"), False
    waited = run_command([*LAUNCHER, "wait", name])
    first, _, answer = waited.stdout.partition("\n")
    code = RC.match(first)
    rc = code[1] if code else f"unknown (wait exit {waited.returncode})"
    verdict, counts = summarize(answer)
    return Result(name, head, f"rc={rc}", verdict, counts), rc == "0"


def guarded(run: dict[str, object]) -> tuple[Result, bool]:
    """Run one review; an unexpected error becomes that run's own block."""
    try:
        return run_one(run)
    except Exception as exc:  # noqa: BLE001 -- one run's failure must not hide the others
        state = f"error ({type(exc).__name__})"
        return Result(str(run["name"]), str(run["head"]), state, "none", "none"), False


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
        preflight(runs[0])
    except SpecError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        outcomes = list(pool.map(guarded, runs))
    print("\n".join(render(result) for result, _ in outcomes), end="")
    ended = all(ok for _, ok in outcomes)
    try:
        now, _, _ = pull_request_state(str(runs[0]["repo"]), cast("int", runs[0]["pr"]))
    except SpecError:
        now = "unreadable"
    if now.lower() != str(runs[0]["head"]).lower():
        print(f"head moved to {now}: every verdict above is void", file=sys.stderr)
        return 1
    return 0 if ended else 1
