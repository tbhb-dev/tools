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
from hashlib import sha256
from itertools import islice
from pathlib import Path
from shlex import quote
from time import monotonic, sleep, time
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.runfiles import atomic, private_dir

if TYPE_CHECKING:
    from typing import IO

COMPONENT_CHARS = r"[A-Za-z0-9._][A-Za-z0-9._-]*"
COMPONENT = re.compile(rf"(?!\.{{1,2}}$){COMPONENT_CHARS}")
HOST = re.compile(rf"(?:(?!\.{{1,2}}@){COMPONENT_CHARS}@)?{COMPONENT.pattern}")
ANSI = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[0-~])"
)
SUMMARY = re.compile(
    rb"FAILED|TIMEOUT|NO STATUS|Build did NOT|Build completed|FLAKY|PASSED|"
    rb"tests pass|^Executed"
)
RUN_SUMMARY = re.compile(rb"tests pass|FAILED|Build did NOT")
BAZEL_START = "GATE-BAZEL-START"
BAZEL_START_LINE = f"{BAZEL_START}\n".encode()
SSH_ERROR = 255
PID_FILE = "pid"
HEARTBEAT_FILE = "heartbeat"
EXIT_FILE = "exit"
REASON_FILE = "reason"
SUPERSEDED_FILE = "superseded"
HEARTBEAT_INTERVAL = 10
STARTUP_GRACE = 5
STALE_LIMIT = 90
POLL_INTERVAL = 15


def heartbeat(directory: Path) -> None:
    stamp = datetime.fromtimestamp(time(), UTC).isoformat()
    atomic(directory / HEARTBEAT_FILE, f"{stamp}\n")


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


def print_run_summary(log: Path) -> None:
    with log.open("rb") as source:
        lines = (ANSI.sub(b"", raw) for raw in source)
        summary = b"".join(
            islice((line for line in lines if RUN_SUMMARY.search(line)), 5)
        )
    if summary:
        sys.stdout.buffer.write(summary.removesuffix(b"\n") + b"\n")


def begin_run(directory: Path) -> Path:
    private_dir(directory)
    superseded = directory / SUPERSEDED_FILE
    # Keep the previous files through preparation without serving them to waiters.
    superseded.touch()
    for name in (PID_FILE, HEARTBEAT_FILE, EXIT_FILE, REASON_FILE):
        (directory / name).unlink(missing_ok=True)
    atomic(directory / PID_FILE, f"{os.getpid()}\n")
    heartbeat(directory)
    return superseded


def remote_commands(
    args: argparse.Namespace, bundle_name: str, sha: str
) -> tuple[str, str]:
    repo = f"~/Code/github.com/tbhb-dev/{quote(args.repo)}"
    remote_bundle = f"~/{quote(bundle_name)}"
    ref = quote(f"refs/gate/{args.name}")
    worktree = quote(f".worktrees/{args.name}")
    bazel = f'printf "\\n{BAZEL_START}\\n" && exec bazelisk test --config=ci //...'
    test = (
        f"cd {repo} && git fetch -q origin && "
        f"git fetch -q {remote_bundle} HEAD:{ref} && "
        f'test "$(git rev-parse {ref})" = {quote(sha)} && '
        f"git worktree add -q --detach {worktree} {ref} && "
        f"cd {worktree} && bash -lc {quote(bazel)}"
    )
    cleanup = (
        f"cd {repo} && {{ git worktree remove --force {worktree}; "
        f"git update-ref -d {ref}; }}; rm -f {remote_bundle}"
    )
    return test, cleanup


def run(args: argparse.Namespace, directory: Path, log: Path, result: Path) -> int:
    superseded = begin_run(directory)
    git = ["git", "-C", args.worktree]
    bundle_name = f"{args.name}.bundle"
    bundle = log.parent / "gate-bundles" / bundle_name
    env = {**os.environ, "SSH_AUTH_SOCK": ""}
    ssh = ["ssh", "-o", "IdentityAgent=none", args.host]
    destination = f"{args.host}:{bundle_name}"
    scp = ["scp", "-q", "-o", "IdentityAgent=none", str(bundle), destination]

    step = "read HEAD"
    published_sha = None
    published_log = None
    with tempfile.NamedTemporaryFile(
        dir=directory,
        delete=False,  # pragma: no mutate - None also disables deletion.
    ) as output:
        try:
            sha = subprocess.check_output(  # nosec B603
                [*git, "rev-parse", "HEAD"], stderr=output, timeout=10, text=True
            ).strip()
            step = "create bundle"
            private_dir(bundle.parent)
            create = [*git, "bundle", "create", str(bundle), "origin/main..HEAD"]
            run_step(create, directory, output).check_returncode()
            remote, cleanup = remote_commands(args, bundle_name, sha)
            try:
                step = "scp bundle"
                run_step(scp, directory, output, env=env).check_returncode()
                result.unlink(missing_ok=True)
                superseded.unlink()
                published_log = log
                step = "ssh test"
                offset = output.tell()
                status = run_step([*ssh, remote], directory, output, env=env).returncode
                output.seek(offset)
                published_sha = (
                    sha if status != SSH_ERROR and BAZEL_START_LINE in output else None
                )
            finally:
                with suppress(OSError):
                    run_step([*ssh, cleanup], directory, subprocess.DEVNULL, env=env)
        except subprocess.CalledProcessError as exc:
            status = exc.returncode
        except OSError, subprocess.TimeoutExpired:
            status = 1

    if published_log is None:
        Path(output.name).unlink()
    else:
        Path(output.name).replace(published_log)
    if published_sha is None:
        atomic(directory / REASON_FILE, f"{step} failed\n")
        status = status or 1
    atomic(directory / EXIT_FILE, f"{status}\n")
    if published_sha is not None:
        atomic(result, f"{published_sha}\n")
        print_run_summary(log)
    return status


def read_state(path: Path) -> str | None:
    with suppress(OSError, UnicodeError):
        return path.read_text(encoding="utf-8").strip()
    return None


def result_status(directory: Path, log: Path, result: Path, sha: str) -> int | None:
    if (directory / SUPERSEDED_FILE).exists():
        return None
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
    status = read_state(directory / EXIT_FILE)
    print(f"exit status: {status if status is not None else 'unknown'}")
    return 0 if status == "0" else 1


def running_failure(directory: Path) -> str | None:
    no_pid = "no pid file (the gate never started or was removed)"
    pid_file = directory / PID_FILE
    pid = 0
    with suppress(ValueError):
        pid = int(read_state(pid_file) or 0)
    if pid <= 0:
        return no_pid
    try:
        os.kill(pid, 0)
    except OverflowError:
        return no_pid
    except ProcessLookupError:
        message = f"pid {pid} is dead and no result was written"
        for name in (EXIT_FILE, REASON_FILE):
            if detail := read_state(directory / name):
                message += f"; {name}: {detail}"
        return message
    except PermissionError:
        pass

    if (beat := read_state(directory / HEARTBEAT_FILE)) is not None:
        with suppress(TypeError, ValueError):
            stamp = datetime.fromisoformat(beat)
            age = (datetime.fromtimestamp(time(), UTC) - stamp).total_seconds()
            stale = f"heartbeat stale (age {age:.0f}s, limit {STALE_LIMIT} s)"
            return stale if age > STALE_LIMIT else None
    with suppress(OSError):
        if time() - pid_file.stat().st_mtime < STARTUP_GRACE:
            return None
    return "heartbeat stale (missing)"


def wait(args: argparse.Namespace, directory: Path, log: Path, result: Path) -> int:
    deadline = monotonic() + args.timeout
    while True:
        if (status := result_status(directory, log, result, args.sha)) is not None:
            return status
        if failure := running_failure(directory):
            if (status := result_status(directory, log, result, args.sha)) is not None:
                return status
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
    run_parser = commands.add_parser(
        "run",
        help="run tests",
        allow_abbrev=False,  # pragma: no mutate - None also disables abbreviations.
    )
    run_parser.add_argument("name", metavar="NAME")
    run_parser.add_argument("worktree", metavar="WORKTREE")
    run_parser.add_argument("host", metavar="HOST")
    run_parser.add_argument("repo", metavar="REPO", nargs="?")
    wait_parser = commands.add_parser(
        "wait",
        help="wait for a result",
        allow_abbrev=False,  # pragma: no mutate - None also disables abbreviations.
    )
    wait_parser.add_argument("name", metavar="NAME")
    wait_parser.add_argument("host", metavar="HOST")
    wait_parser.add_argument("--sha", required=True)
    wait_parser.add_argument("--timeout", metavar="SECONDS", type=int, default=14400)
    try:
        args = parser.parse_args(argv)
        if not COMPONENT.fullmatch(args.name):
            parser.error("invalid NAME")
        if not HOST.fullmatch(args.host):
            parser.error("invalid HOST")
        if args.command == "run":
            args.repo = args.repo or "tools"
            if not COMPONENT.fullmatch(args.repo):
                parser.error("invalid REPO")
        elif args.timeout < 0:
            parser.error("--timeout must be nonnegative")
    except SystemExit as exc:
        return int(exc.code or 0)

    state = Path.home() / ".local" / "state" / "tbhb"
    key = f"{args.name}-{args.host}"
    if "@" in args.host:
        destination = f"{args.name}/{args.host}".encode()
        key = f"ssh.{sha256(destination).hexdigest()}"
    log = state / f"tools-{key}.log"
    action = run if args.command == "run" else wait
    return action(args, state / "gate" / key, log, log.with_suffix(".log.sha"))
