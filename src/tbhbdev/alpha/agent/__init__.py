"""Argument parsing and the dispatch point for the coordinator and run-host."""

from __future__ import annotations

import argparse
import re
import sys

from tbhbdev import userconfig
from tbhbdev.alpha.agent import driver, prune, settings
from tbhbdev.alpha.agent.decisions import config

__all__ = ["driver", "main", "settings"]


def duration(text: str) -> int:
    value = config.parse_duration(text)
    if value is None:
        message = "must be bare seconds or a number ending in s, m or h, up to 7 days"
        raise argparse.ArgumentTypeError(message)
    return value


def parser(prog: str) -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog=prog)
    verbs = command.add_subparsers(dest="verb", required=True)
    pruning = verbs.add_parser("prune", allow_abbrev=False)
    pruning.add_argument("--older-than", type=prune_number, default=14)
    pruning.add_argument("--apply", action="store_true")
    pruning.add_argument("--force", action="store_true")
    pruning.add_argument("--largest", type=prune_positive)
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
    return command


def prune_number(text: str) -> int:
    if re.fullmatch(r"[0-9]{1,6}", text) is None:
        message = "must be a nonnegative integer of at most six digits"
        raise argparse.ArgumentTypeError(message)
    return int(text)


def prune_positive(text: str) -> int:
    value = prune_number(text)
    if value == 0:
        message = "must be greater than zero"
        raise argparse.ArgumentTypeError(message)
    return value


def main(argv: list[str], prog: str) -> int:
    try:
        command = parser(prog)
        args = command.parse_args(argv)
        if (
            args.verb == "prune"
            and args.largest is not None
            and (args.apply or args.force)
        ):
            command.error("--largest cannot accompany --apply or --force")
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        # Phase 3a dispatches its hidden run-host verb here.
        if args.verb == "prune":
            return prune.main(settings.load(), args)
        return driver.dispatch(settings.load(), args)
    except (userconfig.ConfigError, OSError, ValueError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
