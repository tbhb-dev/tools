"""Run a Bazel gate on a host and wait for a specific commit's result."""

import argparse
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from shlex import quote
from time import monotonic, sleep, time
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.runfiles import atomic, private_dir

if TYPE_CHECKING:
    from typing import IO

COMPONENT = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._][A-Za-z0-9._-]*")
ANSI = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[0-~])"
)
SUMMARY = re.compile(
    rb"FAILED|TIMEOUT|NO STATUS|Build did NOT|Build completed|FLAKY|PASSED|"
    rb"tests pass|^Executed"
)
HEARTBEAT_INTERVAL = 10
STALE_LIMIT = 90
POLL_INTERVAL = 15


def heartbeat(directory: Path) -> None:
    stamp = datetime.fromtimestamp(time(), UTC).isoformat()
    atomic(directory / "heartbeat", f"{stamp}\n")


def run_step(
    command: list[str],
    directory: Path,
    stdout: IO[bytes] | int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    heartbeat(directory)
    # Callers supply fixed programs and quote validated SSH arguments.
    with subprocess.Popen(  # nosec B603
        command, stdout=stdout, stderr=subprocess.STDOUT, env=env
    ) as process:
        while True:
            with suppress(subprocess.TimeoutExpired):
                status = process.wait(timeout=HEARTBEAT_INTERVAL)
                return subprocess.CompletedProcess(command, status)
            heartbeat(directory)


def run(args: argparse.Namespace, directory: Path, log: Path, result: Path) -> int:
    private_dir(directory)
    old_state = (directory / name for name in ("pid", "heartbeat", "exit", "reason"))
    for old in (result, log, *old_state):
        old.unlink(missing_ok=True)
    atomic(directory / "pid", f"{os.getpid()}\n")
    heartbeat(directory)

    git = ["git", "-C", args.worktree]
    bundle_name = f"{args.name}.bundle"
    bundle = log.parent / "gate-bundles" / bundle_name
    repo = f"~/Code/github.com/tbhb-dev/{quote(args.repo)}"
    remote_bundle = f"~/{quote(bundle_name)}"
    ref = quote(f"refs/gate/{args.name}")
    worktree = quote(f".worktrees/{args.name}")
    env = {**os.environ, "SSH_AUTH_SOCK": ""}
    ssh = ["ssh", "-o", "IdentityAgent=none", args.host]
    destination = f"{args.host}:{bundle_name}"
    scp = ["scp", "-q", "-o", "IdentityAgent=none", str(bundle), destination]
    cleanup = (
        f"cd {repo} && {{ git worktree remove --force {worktree}; "
        f"git update-ref -d {ref}; }}; rm -f {remote_bundle}"
    )

    step = "read HEAD"
    sha = ""
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
        try:
            sha = subprocess.check_output(  # nosec B603
                [*git, "rev-parse", "HEAD"], stderr=output, timeout=10, text=True
            ).strip()
            step = "create bundle"
            private_dir(bundle.parent)
            create = [*git, "bundle", "create", str(bundle), "origin/main..HEAD"]
            run_step(create, directory, output).check_returncode()
            try:
                step = "scp bundle"
                run_step(scp, directory, output, env=env).check_returncode()
                step = "ssh test"
                remote = (
                    f"cd {repo} && git fetch origin && "
                    f"git fetch {remote_bundle} HEAD:{ref} && "
                    f'test "$(git rev-parse {ref})" = {quote(sha)} && '
                    f"git worktree add --detach {worktree} {ref} && cd {worktree} && "
                    "bash -lc 'bazelisk test --config=ci //...'"
                )
                status = run_step([*ssh, remote], directory, output, env=env).returncode
            finally:
                with suppress(OSError):
                    run_step([*ssh, cleanup], directory, subprocess.DEVNULL, env=env)
        except subprocess.CalledProcessError as exc:
            status = exc.returncode
        except OSError, subprocess.TimeoutExpired:
            status = 1
        else:
            step = ""

    Path(output.name).replace(log)
    if step:
        atomic(directory / "reason", f"{step} failed\n")
    atomic(directory / "exit", f"{status}\n")
    if not step:
        atomic(result, f"{sha}\n")
    return status


def read_state(path: Path) -> str | None:
    with suppress(OSError, UnicodeError):
        return path.read_text(encoding="utf-8").strip()
    return None


def result_status(directory: Path, log: Path, result: Path, sha: str) -> int | None:
    actual = read_state(result)
    if actual is None:
        return None
    if actual != sha:
        print(f"FAILED: result is for sha {actual}, expected {sha}")
        return 1
    lines: deque[bytes] = deque(maxlen=40)
    with suppress(OSError), log.open("rb") as source:
        for raw in source:
            line = ANSI.sub(b"", raw)
            if SUMMARY.search(line):
                lines.append(line)
    if lines:
        sys.stdout.buffer.write(b"".join(lines).removesuffix(b"\n") + b"\n")
    status = read_state(directory / "exit")
    print(f"exit status: {status if status is not None else 'unknown'}")
    return 0 if status == "0" else 1


def running_failure(directory: Path) -> str | None:
    no_pid = "no pid file (the gate never started or was removed)"
    pid = 0
    with suppress(ValueError):
        pid = int(read_state(directory / "pid") or "0")
    if pid <= 0:
        return no_pid
    try:
        os.kill(pid, 0)
    except OverflowError:
        return no_pid
    except ProcessLookupError:
        message = f"pid {pid} is dead and no result was written"
        for name in ("exit", "reason"):
            if detail := read_state(directory / name):
                message += f"; {name}: {detail}"
        return message
    except PermissionError:
        pass

    try:
        stamp = datetime.fromisoformat(read_state(directory / "heartbeat") or "")
        age = (datetime.fromtimestamp(time(), UTC) - stamp).total_seconds()
    except TypeError, ValueError:
        return "heartbeat stale (missing)"
    stale = f"heartbeat stale (age {age:.0f}s, limit {STALE_LIMIT} s)"
    return stale if age > STALE_LIMIT else None


def wait(args: argparse.Namespace, directory: Path, log: Path, result: Path) -> int:
    deadline = monotonic() + args.timeout
    while True:
        if (status := result_status(directory, log, result, args.sha)) is not None:
            return status
        if failure := running_failure(directory):
            print(f"FAILED: {failure}")
            return 1
        remaining = deadline - monotonic()
        if remaining <= 0:
            print(f"FAILED: timeout after {args.timeout} s, gate still running")
            return 3
        sleep(min(POLL_INTERVAL, remaining))


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run tests", allow_abbrev=False)
    run_parser.add_argument("name", metavar="NAME")
    run_parser.add_argument("worktree", metavar="WORKTREE")
    run_parser.add_argument("host", metavar="HOST")
    run_parser.add_argument("repo", metavar="REPO", nargs="?", default="tools")
    wait_parser = commands.add_parser(
        "wait", help="wait for a result", allow_abbrev=False
    )
    wait_parser.add_argument("name", metavar="NAME")
    wait_parser.add_argument("host", metavar="HOST")
    wait_parser.add_argument("--sha", metavar="SHA", required=True)
    wait_parser.add_argument("--timeout", metavar="SECONDS", type=int, default=14400)
    try:
        args = parser.parse_args(argv)
        for field in ("name", "host", "repo"):
            if not COMPONENT.fullmatch(getattr(args, field, "tools")):
                parser.error(f"invalid {field.upper()}")
        if args.command == "wait" and args.timeout < 0:
            parser.error("--timeout must be nonnegative")
    except SystemExit as exc:
        return int(exc.code or 0)

    state = Path.home() / ".local" / "state" / "tbhb"
    key = f"{args.name}-{args.host}"
    log = state / f"tools-{key}.log"
    action = run if args.command == "run" else wait
    return action(args, state / "gate" / key, log, log.with_suffix(".log.sha"))
