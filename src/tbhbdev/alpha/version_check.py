"""`tbhbdev alpha version-check`: compare the installed build on each host.

Read-only. For each host it runs `tbhbdev --version` (over ssh, or directly on
this host), takes the build stamp from the output and prints one line:
`<host> <sha-or-unknown> ok|stale|unknown|unreachable`.

Hosts come from `--host`, else the names in `[harness] hosts` of the user
config. Without `--expect`, the reference is the build most hosts
run (the first host's on a tie). `--expect latest` reads origin's main.

Exit status: 0 when every host is ok, 1 when any host is stale or unknown, 2
for a usage error, 3 when a host is unreachable and none is stale.
"""

import argparse
import json
import re
import socket
import subprocess  # nosec B404
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from tbhbdev import userconfig, version
from tbhbdev.alpha import session_context

REPO_URL = "https://github.com/tbhb-dev/tools"
SSH = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5")
# Plain ssh has no ~/.local/bin on PATH, and the remote shell expands $HOME.
REMOTE_COMMAND = "$HOME/.local/bin/tbhbdev --version"
SSH_FAILED = 255
TIMEOUT = 30
HOST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
SHA = re.compile(r"[0-9a-f]{40}")
LS_REMOTE = re.compile(r"([0-9a-f]{40})\trefs/heads/main")
VERSION_LINE = re.compile(r"tbhbdev \S+ \((.*)\)")

Runner = Callable[[list[str]], tuple[int, str]]


def run(argv: list[str]) -> tuple[int, str]:
    """Run ARGV and return its exit status and stdout; 255 when it can't run."""
    try:
        done = subprocess.run(  # nosec B603
            argv, capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except OSError, subprocess.TimeoutExpired:
        return SSH_FAILED, ""
    return done.returncode, done.stdout


def local_name() -> str:
    """This host's short name, lowercase."""
    return socket.gethostname().split(".")[0].lower()


def default_hosts() -> list[str]:
    """The hosts of the user config, or none."""
    entries = userconfig.optional_strings(session_context.TABLE, "hosts")
    return list(session_context.parse_hosts(entries)) if entries else []


def parse_stamp(output: str) -> str | None:
    """The 40-hex build in `tbhbdev --version` OUTPUT, or None."""
    found = VERSION_LINE.search(output)
    if found and SHA.fullmatch(found.group(1)):
        return found.group(1)
    return None


def probe(host: str, runner: Runner, here: str) -> tuple[str | None, bool]:
    """HOST's build stamp (or None) and whether HOST could be reached."""
    if host.lower() == here:
        argv = [str(Path.home() / ".local" / "bin" / "tbhbdev"), "--version"]
    else:
        argv = [*SSH, host, REMOTE_COMMAND]
    status, output = runner(argv)
    if status == SSH_FAILED:
        return None, False
    return (parse_stamp(output) if status == 0 else None), True


def latest(runner: Runner) -> str | None:
    """The commit at the head of origin's main, or None when it can't be read."""
    status, output = runner(["git", "ls-remote", REPO_URL, "refs/heads/main"])
    found = LS_REMOTE.match(output) if status == 0 else None
    return found.group(1) if found else None


def reference(stamps: list[str | None]) -> str | None:
    """The build most hosts run, the earliest host's on a tie."""
    counts = Counter(stamp for stamp in stamps if stamp is not None)
    return counts.most_common(1)[0][0] if counts else None


def state(stamp: str | None, *, reached: bool, expect: str | None) -> str:
    """ok, stale, unknown or unreachable for one host."""
    if not reached:
        return "unreachable"
    if stamp is None:
        return "unknown"
    return "ok" if stamp == expect else "stale"


def exit_status(states: list[str]) -> int:
    """0 all ok, 1 any stale or unknown, 3 any unreachable, else 0."""
    if "stale" in states or "unknown" in states:
        return 1
    return 3 if "unreachable" in states else 0


def parser(prog: str) -> argparse.ArgumentParser:
    """The command line."""
    result = argparse.ArgumentParser(prog=prog, description=__doc__)
    result.add_argument("--host", action="append", default=[], metavar="NAME")
    result.add_argument("--expect", metavar="SHA|latest")
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str], prog: str, runner: Runner = run) -> int:
    """Check each host's build and print one line per host."""
    try:
        args = parser(prog).parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return check(args, prog, runner)


def invalid(args: argparse.Namespace, hosts: list[str]) -> str | None:
    """Why the hosts or `--expect` are unusable, or None."""
    bad = [host for host in hosts if not HOST_NAME.fullmatch(host)]
    if bad:
        return f"not a host name: {bad[0]!r}"
    if args.expect not in {None, "latest"} and not SHA.fullmatch(args.expect):
        return f"--expect wants 40 hex digits or latest: {args.expect!r}"
    return None


def check(args: argparse.Namespace, prog: str, runner: Runner) -> int:
    """Run the check for parsed ARGS."""
    try:
        hosts = args.host or default_hosts()
    except userconfig.ConfigError as exc:
        return fail(prog, str(exc), 2)
    if not hosts:
        return fail(prog, "no hosts: pass --host or set [harness] hosts", 2)
    problem = invalid(args, hosts)
    if problem is not None:
        return fail(prog, problem, 2)
    expect = args.expect
    if expect == "latest":
        expect = latest(runner)
        if expect is None:
            return fail(prog, f"cannot read main of {REPO_URL}", 3)
    probes = [probe(host, runner, local_name()) for host in hosts]
    if expect is None:
        expect = reference([stamp for stamp, _ in probes])
    states = [state(stamp, reached=reached, expect=expect) for stamp, reached in probes]
    report(hosts, [stamp for stamp, _ in probes], states, as_json=args.json)
    return exit_status(states)


def fail(prog: str, message: str, status: int) -> int:
    """Print MESSAGE to stderr and return STATUS."""
    print(f"{prog}: {message}", file=sys.stderr)
    return status


def report(
    hosts: list[str], stamps: list[str | None], states: list[str], *, as_json: bool
) -> None:
    """Print one line per host, or one JSON list."""
    builds = [stamp or version.UNKNOWN for stamp in stamps]
    if as_json:
        rows = [
            {"host": host, "version": build, "state": state}
            for host, build, state in zip(hosts, builds, states, strict=True)
        ]
        print(json.dumps(rows))
        return
    for host, build, state in zip(hosts, builds, states, strict=True):
        print(f"{host} {build} {state}")
