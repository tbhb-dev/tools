"""Argument parsing and the dispatch point for the coordinator and run-host."""

from __future__ import annotations

import argparse
import sys

from tbhbdev import telemetry, userconfig
from tbhbdev.alpha import worker_identity
from tbhbdev.alpha.agent import driver, precheck, settings
from tbhbdev.alpha.agent import run_host as run_host
from tbhbdev.alpha.agent import signing_parity as signing_parity
from tbhbdev.alpha.agent import ssh_serve as ssh_serve
from tbhbdev.alpha.agent.decisions import config

__all__ = ["driver", "main", "precheck", "settings"]


def duration(text: str) -> int:
    value = config.parse_duration(text)
    if value is None:
        message = "must be bare seconds or a number ending in s, m or h, up to 7 days"
        raise argparse.ArgumentTypeError(message)
    return value


def parser(prog: str) -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog=prog)
    verbs = command.add_subparsers(dest="verb", required=True)
    execute = verbs.add_parser("exec", help="launch a headless run")
    execute.add_argument("--host", required=True)
    execute.add_argument("--repo", required=True)
    execute.add_argument("--ref")
    execute.add_argument(
        "--fetch-ref",
        action="append",
        default=[],
        metavar="REF",
        help="fetch refs/heads/<branch> as origin/fetched/heads/<branch> or "
        "refs/pull/N/head as origin/fetched/pull/N/head (repeat up to 8 times; "
        "any profile)",
    )
    execute.add_argument(
        "--start-ref",
        metavar="REF",
        help="start at a refs/heads/<branch> also listed with --fetch-ref "
        "and fast-forward that branch without opening a pull request "
        "(build profile only; excludes the default branch)",
    )
    execute.add_argument(
        "--harness",
        choices=("codex", "claude", "agy"),
        default="codex",
        help="harness to launch: Codex, Claude or agy",
    )
    execute.add_argument(
        "--allow-non-gemini",
        action="store_true",
        help="let agy run a non-Gemini model ID",
    )
    execute.add_argument(
        "--ignore-limits",
        action="store_true",
        help="launch although the host's load, memory or swap exceeds its limits",
    )
    execute.add_argument(
        "--expect-commit",
        action=argparse.BooleanOptionalAction,
        help="require a new commit and a push (default for builds); "
        "--no-expect-commit waives both and the Stop hook accepts a clean build",
    )
    accounts = execute.add_mutually_exclusive_group()
    accounts.add_argument("--account")
    accounts.add_argument("--ops", dest="account", action="store_const", const="ops")
    execute.add_argument(
        "--model",
        required=True,
        help="full model ID. agy requires a gemini-* ID such as gemini-4-argon-high. "
        "Claude requires a full ID such as claude-opus-5-5",
    )
    execute.add_argument("--effort", required=True)
    execute.add_argument("--profile", choices=("build", "review"), required=True)
    execute.add_argument("--reason")
    execute.add_argument("--claude-profile")
    execute.add_argument(
        "--unsandboxed",
        help="authorization: owner/repo#N or operator-chat-YYYY-MM-DD",
    )
    execute.add_argument("--timeout", type=duration)
    execute.add_argument("--schema")
    execute.add_argument("--class", dest="class_")
    execute.add_argument("--variant")
    execute.add_argument("--role")
    execute.add_argument(
        "--worker-workflow",
        type=worker_identity.workflow,
        metavar="ID",
        help="launch as worker/ID: the caller must descend from the "
        "executable in the config's [agent.worker] table (no tmux pane)",
    )
    execute.add_argument("name")
    execute.add_argument("prompt_file")
    for verb in ("wait", "status", "kill", "clean", "keep", "log", "diff"):
        sub = verbs.add_parser(verb)
        sub.add_argument("name")
        if verb == "log":
            sub.add_argument("-n", type=int, default=10)
        if verb == "clean":
            sub.add_argument("--force", action="store_true")
    verbs.add_parser(
        "signing-parity", help="check each host's signing key is registered"
    )
    return command


def main(argv: list[str], prog: str) -> int:
    host_commands = {
        "run-host": run_host.main,
        "ssh-serve": ssh_serve.main,
        "precheck": precheck.main,
        "signing-fingerprint": signing_parity.host_main,
    }
    if argv and argv[0] in host_commands:
        return host_commands[argv[0]](argv[1:], f"{prog} {argv[0]}")
    try:
        args = parser(prog).parse_args(argv) if argv else None
    except SystemExit as exc:
        return int(exc.code or 0)
    if args is not None and args.verb == "exec":
        return telemetry.command(lambda: dispatch(args, prog))
    return dispatch(args, prog)


def dispatch(args: argparse.Namespace | None, prog: str) -> int:
    try:
        loaded = settings.load()
        if args is None:
            run_host.check_config(loaded)
            print(f"{prog}: configuration valid")
            return 0
        return driver.dispatch(loaded, args)
    except (userconfig.ConfigError, OSError, ValueError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
