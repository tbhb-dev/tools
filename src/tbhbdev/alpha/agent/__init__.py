"""Argument parsing and the dispatch point for the coordinator and run-host."""

from __future__ import annotations

import argparse
import sys

from tbhbdev import userconfig
from tbhbdev.alpha.agent import driver, precheck, settings
from tbhbdev.alpha.agent import run_host as run_host
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
        "--harness", choices=("codex", "claude", "agy"), default="codex"
    )
    execute.add_argument("--allow-non-gemini", action="store_true")
    accounts = execute.add_mutually_exclusive_group()
    accounts.add_argument("--account")
    accounts.add_argument("--ops", dest="account", action="store_const", const="ops")
    execute.add_argument("--model", required=True)
    execute.add_argument("--effort", required=True)
    execute.add_argument("--profile", choices=("build", "review"), required=True)
    execute.add_argument("--reason")
    execute.add_argument("--claude-profile")
    execute.add_argument("--unsandboxed")
    execute.add_argument("--timeout", type=duration)
    execute.add_argument("--schema")
    execute.add_argument("--class", dest="class_")
    execute.add_argument("--variant")
    execute.add_argument("--role")
    execute.add_argument("name")
    execute.add_argument("prompt_file")
    for verb in ("wait", "status", "kill", "clean", "keep", "log", "diff"):
        sub = verbs.add_parser(verb)
        sub.add_argument("name")
        if verb == "log":
            sub.add_argument("-n", type=int, default=10)
        if verb == "clean":
            sub.add_argument("--force", action="store_true")
    clear = verbs.add_parser("clear-taint", help="clear local host taint with a reason")
    clear.add_argument("host")
    clear.add_argument("--reason", required=True)
    return command


def main(argv: list[str], prog: str) -> int:
    if argv and argv[0] == "run-host":
        return run_host.main(argv[1:], f"{prog} run-host")
    if argv and argv[0] == "precheck":
        return precheck.main(argv[1:], f"{prog} precheck")
    try:
        args = parser(prog).parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return driver.dispatch(settings.load(), args)
    except (userconfig.ConfigError, OSError, ValueError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
