"""Check a worktree's review policy files against the org floor and closed schemas."""

import argparse
import json
import re
import sys
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    from collections.abc import Callable

TOML = Path(".github") / "pull_request_review.toml"
TEMPLATE = Path(".github") / "PULL_REQUEST_REVIEW_TEMPLATE.md"
CHECKLIST = Path(".github") / "PULL_REQUEST_REVIEW_CHECKLIST.md"
DRAFT_SCHEMA = Path(".github") / "schemas" / "pull-request-review-draft.schema.json"
GUIDANCE = re.compile(r"<!--\s*(Required|Optional)(?:, at most (\d+) words)?\.")
ITEM = re.compile(r"^- \*\*(R|P|X|K|E)(\d+)\*\*", re.MULTILINE)
FLOOR_ESCALATION = re.compile(r"FE\d+")
CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema"],
    "properties": {
        "schema": {"const": 1},
        "review": {"enum": ["required", "advisory", "none"]},
        "max_diff_lines": {"type": "integer", "minimum": 1},
        "merge": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"enum": ["auto", "owner"]},
                "owner": {"type": "string"},
                "retest": {"enum": ["if_base_moved", "never"]},
            },
            "if": {"required": ["mode"], "properties": {"mode": {"const": "owner"}}},
            "then": {"required": ["owner"], "properties": {"owner": {"minLength": 1}}},
        },
    },
}


class Finding(NamedTuple):
    """One problem in a repository's review file."""

    path: str
    rule: str
    message: str


def sections(text: str) -> list[tuple[str, str | None]]:
    """Each H2 heading with the Required or Optional word of its guidance comment."""
    blocks = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    result = []
    for block in blocks:
        heading, _, rest = block.partition("\n")
        match = GUIDANCE.match(rest.lstrip())
        result.append((heading.strip(), match and match[1]))
    return result


def floor_required() -> list[str]:
    """The floor template's required sections, in order."""
    text = (
        files("tbhbdev.alpha")
        .joinpath("review_data", TEMPLATE.name)
        .read_text(encoding="utf-8")
    )
    return [name for name, kind in sections(text) if kind == "Required"]


def check_config(text: str) -> list[Finding]:
    """The TOML must parse and meet the closed config schema."""
    path = str(TOML)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [Finding(path, "config-toml", str(exc))]
    validator = Draft202012Validator(CONFIG_SCHEMA)
    return [
        Finding(
            path, "config-schema", f"{'/'.join(map(str, e.path)) or '.'}: {e.message}"
        )
        for e in sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    ]


def check_template(text: str, required: list[str]) -> list[Finding]:
    """A template keeps the floor's required sections, in order, and guides each."""
    path = str(TEMPLATE)
    found = sections(text)
    findings = [
        Finding(path, "template-guidance", f"section {name} has no guidance comment")
        for name, kind in found
        if kind is None
    ]
    names = iter(name for name, _ in found)
    findings += [
        Finding(
            path,
            "template-floor-section",
            f"required section {name} is missing or out of order",
        )
        for name in required
        if name not in names
    ]
    return findings


def check_draft_schema(text: str) -> list[Finding]:
    """A repository's draft schema must be valid JSON Schema 2020-12 and closed."""
    path = str(DRAFT_SCHEMA)
    try:
        schema = json.loads(text)
        Draft202012Validator.check_schema(schema)
    except (ValueError, SchemaError) as exc:
        return [Finding(path, "draft-schema-invalid", " ".join(str(exc).split())[:200])]
    if schema.get("additionalProperties") is not False:
        return [
            Finding(path, "draft-schema-open", "additionalProperties must be false")
        ]
    return []


def check_checklist(text: str) -> list[Finding]:
    """Item and escalation ids must be unique, and the floor's ranges stay reserved."""
    path = str(CHECKLIST)
    try:
        meta, body = pr.parse_frontmatter(text)
    except pr.FrontmatterError as exc:
        return [Finding(path, "checklist-frontmatter", str(exc))]
    findings = []
    seen = set()
    for kind, number in ITEM.findall(body):
        item = kind + number
        if item in seen:
            findings.append(Finding(path, "checklist-item-id", f"{item} appears twice"))
        if kind == "R" and int(number) < 100:  # noqa: PLR2004 -- R1 to R99 are the floor's
            findings.append(
                Finding(
                    path,
                    "checklist-item-id",
                    f"{item} is in the floor's reserved range",
                )
            )
        seen.add(item)
    fields = cast("dict[str, object]", meta) if isinstance(meta, dict) else {}
    entries = cast("list[object]", fields.get("escalate"))
    ids = [
        cast("dict[str, object]", e).get("id")
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict)
    ]
    findings += [
        Finding(
            path,
            "checklist-escalate-id",
            f"escalation id {entry!r} is not unique or is reserved",
        )
        for entry in ids
        if not isinstance(entry, str)
        or FLOOR_ESCALATION.fullmatch(entry)
        or ids.count(entry) > 1
    ]
    return findings


def check(root: Path) -> list[Finding]:
    """Check each review file the worktree has. A missing file means the floor."""
    required = floor_required()
    findings: list[Finding] = []
    checks: list[tuple[Path, Callable[[str], list[Finding]]]] = [
        (TOML, check_config),
        (TEMPLATE, lambda text: check_template(text, required)),
        (CHECKLIST, check_checklist),
        (DRAFT_SCHEMA, check_draft_schema),
    ]
    for path, run in checks:
        try:
            text = (root / path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            findings.append(Finding(str(path), "unreadable", str(exc)))
            continue
        findings += run(text)
    return findings


def main(argv: list[str], prog: str) -> int:
    """Run `check`: exit 0 clean, 1 findings, 2 if the floor or path fails to load."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("check")
    cmd.add_argument("path", nargs="?", default=".")
    cmd.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.path)
    if not root.is_dir():
        print(f"{prog}: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        findings = check(root)
    except (OSError, UnicodeError) as exc:
        # Only the floor's own package data can raise here: repository reads are caught.
        print(f"{prog}: cannot load the floor: {exc}", file=sys.stderr)
        return 2
    if args.json:
        listed = [
            {"path": f.path, "id": f.rule, "message": f.message} for f in findings
        ]
        print(json.dumps({"ok": not findings, "findings": listed}))
    else:
        for f in findings:
            print(f"{f.path}: {f.rule}: {f.message}")
    return 1 if findings else 0
