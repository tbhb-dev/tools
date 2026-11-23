"""Issue staging commands."""

import argparse
import sys
from pathlib import Path

from tbhbdev import issue_import


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description="Work with issue drafts.")
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("import", help="Check staged issue blocks.")
    stage.add_argument("file", type=Path)
    stage.add_argument("--check", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        blocks, findings = issue_import.read(args.file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        print(f"{prog}: cannot read issue blocks: {exc}", file=sys.stderr)
        return 2
    findings.extend(issue_import.check(blocks))
    for finding in findings:
        print(f"Refused {finding}", file=sys.stderr)
    for block in blocks.values():
        if issue_import.title_warning(block):
            print(f"Warning {block.id}: title shape", file=sys.stderr)
    if findings:
        return 1
    print(f"Checked {len(blocks)} issue blocks.")
    return 0
