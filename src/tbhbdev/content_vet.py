"""Validate normalized content with the pinned CUE schema evaluator."""

import json
import re
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from tbhbdev import cue_runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

type Json = dict[str, object]

_PATTERNS = """{
    epic: #EpicShapeBan, feature: #FeatureShape, task: #TaskShape,
    defect: #DefectShape, ci: #CIRestatement
}"""
_PARENT_TYPES = {"feature": "Epic", "epic": "Initiative"}
_CUE_FIELD = re.compile(r'^((?:"(?:[^"\\]|\\.)*"|[^\s":])+): (.+)$')
_ACCEPTANCE = re.compile(r"acceptance(?:-criteria)?(?:-\d+)?")
_JOINED_CHECKS = re.compile(
    r"\band\s+(?:\w+\s+){0,3}(?:is|are|can|must|has|have|"
    r"(?:appear|display|export|load|open|return|save|send|show|update)s?)\b",
    re.IGNORECASE,
)
_GUIDES = {
    "ci-restatement": "CI already checks this; state the observable outcome instead",
    "epic-title": "Name the capability in a noun phrase, such as Report delivery.",
    "feature-title": "Name a role and action, such as Analysts can download reports.",
    "task-title": "Start with an action verb, such as Document the export endpoint.",
    "defect-title": "Describe the failure and when it occurs.",
    "joined-checks": "Write one acceptance criterion per observable outcome.",
    "parent-type": "Link the issue to a parent of the required type.",
    "unchecked-acceptance": "Complete and tick the acceptance boxes before closing.",
}


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class CheckResult:
    refusals: tuple[Finding, ...] = ()
    warnings: tuple[Finding, ...] = ()


def guide(finding: Finding) -> str:
    """Explain how to revise content that triggered a finding."""
    return _GUIDES.get(finding.code, "Revise the value to meet the schema constraint.")


def format_refusal(finding: Finding) -> str:
    """Render a diagnostic and its revision guidance."""
    location = f"{finding.path}: " if finding.path else ""
    return f"{location}{finding.message}\n{guide(finding)}"


def _cue(
    binary: Path, *arguments: str, data: str | None = None
) -> subprocess.CompletedProcess[str]:
    # The executable and schemas are trusted. Draft data goes on stdin.
    return subprocess.run(  # nosec B603
        [str(binary), *arguments],
        input=data,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )


def _cue_refusals(stderr: str, ci_pattern: str) -> tuple[Finding, ...]:
    refusals = []
    ci_constraint = f" (out of bound !~{json.dumps(ci_pattern)})"
    for line in stderr.splitlines():
        if not line or line[0].isspace():
            continue
        detail = line.removesuffix(":")
        match = _CUE_FIELD.fullmatch(detail)
        path, message = match.groups() if match else ("", detail)
        if message.endswith(ci_constraint):
            refusals.append(Finding("ci-restatement", path, "This repeats a CI gate."))
        else:
            refusals.append(Finding("cue", path, message))
    return tuple(refusals) or (Finding("cue", "", "CUE validation failed."),)


def _acceptance(blocks: dict[str, Json]) -> list[tuple[str, Json]]:
    return [(key, block) for key, block in blocks.items() if _ACCEPTANCE.fullmatch(key)]


def check_close(draft: Json) -> tuple[Finding, ...]:
    """Check acceptance checkboxes without needing CUE or a variant schema."""
    blocks = cast("dict[str, Json]", draft["blocks"])
    return tuple(
        Finding(
            "unchecked-acceptance", f"blocks.{key}", "Acceptance boxes are unchecked."
        )
        for key, block in _acceptance(blocks)
        if cast("Json", block["metrics"])["uncheckedItems"]
    )


def _issue_warnings(
    draft: Json, kind: str, patterns: dict[str, str]
) -> tuple[Finding, ...]:
    warnings = []
    blocks = cast("dict[str, Json]", draft["blocks"])
    shape = "task" if kind == "chore" else kind
    if shape in {"epic", "feature", "task", "defect"}:
        title = cast("str", blocks["title"]["visible"])
        matches = bool(re.search(patterns[shape], title))
        if matches == (shape == "epic"):
            warnings.append(
                Finding(
                    f"{shape}-title", "blocks.title.visible", "Review the title shape."
                )
            )
    for key, block in _acceptance(blocks):
        for index, content in enumerate(cast("list[Json]", block["content"])):
            for number, item in enumerate(cast("list[Json]", content.get("items", []))):
                if _JOINED_CHECKS.search(cast("str", item["visible"])):
                    path = f"blocks.{key}.content.{index}.items.{number}.visible"
                    warnings.append(
                        Finding(
                            "joined-checks",
                            path,
                            "This may join two checks with 'and'.",
                        )
                    )
    return tuple(warnings)


def check(
    draft: Json,
    schemas: Path,
    *,
    variant_schema: Path | Sequence[Path] | None = None,
    definition: str = "#Draft",
    parent_type: str | None = None,
) -> CheckResult:
    """Vet a normalized draft against the shared and optional variant files.

    Supply the trusted schema directory and, for a variant, its ordered files and
    CUE definition. The caller looks up the actual parent issue type.
    CUE must accept the draft before the Python issue rules inspect it.
    """
    binary = cue_runtime.resolve()
    shared = [
        str((schemas / name).resolve())
        for name in ("core.cue", "taxonomy.cue", "issue.cue")
    ]
    variants = (variant_schema,) if isinstance(variant_schema, Path) else variant_schema
    files = [*shared, *(str(path.resolve()) for path in variants or ())]
    vet = _cue(
        binary,
        "vet",
        "--all-errors",
        "-d",
        f"#Draft & ({definition})",
        *files,
        "json:",
        "-",
        data=json.dumps(draft),
    )
    patterns = _cue(binary, "export", *shared, "-e", _PATTERNS, "--out", "json")
    if patterns.returncode:
        return CheckResult(_cue_refusals((vet.stderr or patterns.stderr), ""))
    rules = cast("dict[str, str]", json.loads(patterns.stdout))
    if vet.returncode:
        return CheckResult(_cue_refusals(vet.stderr, rules["ci"]))
    if draft["object"] != "issue":
        return CheckResult()
    kind = cast("str", draft["variant"]).casefold()
    expected = _PARENT_TYPES.get(kind)
    refusals = ()
    if expected and (parent_type or "").casefold() != expected.casefold():
        refusals = (
            Finding("parent-type", "metadata.parent", f"Parent must be an {expected}."),
        )
    return CheckResult(refusals, _issue_warnings(draft, kind, rules))
