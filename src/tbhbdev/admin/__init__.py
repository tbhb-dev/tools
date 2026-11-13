"""Organization administration with an enforced policy and durable audit log."""

import http
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
lazy import argparse

from tbhbdev.admin.audit import RefusedError
from tbhbdev.admin.policy import check as check
from tbhbdev.admin.request import Runtime, Settings, call, default_runtime
from tbhbdev.alpha import gh_app

if TYPE_CHECKING:
    from collections.abc import Sequence

USAGE = """usage: {prog} call METHOD PATH [--json FILE|-] --reason TEXT [--dry-run]
Settings: --app NAME --item NAME --vault NAME --app-id ID
"""
COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def parser() -> argparse.ArgumentParser:
    result = gh_app.Parser()
    commands = result.add_subparsers(dest="command", required=True)
    direct = commands.add_parser("call")
    direct.add_argument("method")
    direct.add_argument("path")
    direct.add_argument("--json")
    direct.add_argument("--reason", required=True)
    direct.add_argument("--dry-run", action="store_true")
    direct.add_argument("--app", default=Settings.app)
    direct.add_argument("--item", default=Settings.item)
    direct.add_argument("--vault", default=Settings.vault)
    direct.add_argument("--app-id", default=Settings.app_id)
    return result


def body_from(source: str | None) -> object:
    if source is None:
        return None
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text()
        return json.loads(text)
    except OSError, ValueError:
        msg = "invalid_body"
        raise RefusedError(msg) from None


def dispatch(runtime: Runtime, args: argparse.Namespace) -> int:
    if not args.reason.strip():
        msg = "reason_required"
        raise RefusedError(msg)
    settings = Settings(args.app, args.item, args.vault, args.app_id)
    if not all(
        COMPONENT.fullmatch(value)
        for value in (settings.app, settings.item, settings.vault, settings.app_id)
    ):
        msg = "invalid_setting"
        raise RefusedError(msg)
    runtime = replace(runtime, settings=settings)
    status, text = call(
        runtime,
        args.method,
        args.path,
        body_from(args.json),
        args.reason,
        dry_run=args.dry_run,
    )
    sys.stdout.write(text)
    return int(status >= http.HTTPStatus.BAD_REQUEST)


def main(argv: Sequence[str], prog: str, *, runtime: Runtime | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return dispatch(runtime or default_runtime(), args)
    except gh_app.HelpRequestedError:
        sys.stdout.write(USAGE.format(prog=prog))
        return 0
    except gh_app.UsageError:
        sys.stderr.write(USAGE.format(prog=prog))
        return 2
    except RefusedError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3
