"""Validate rendered Markdown against the issue schemas in a pm checkout."""

import re
from importlib.resources import as_file, files

# Only the exception type raised by the CUE subprocess is needed here.
from subprocess import TimeoutExpired  # nosec B404
from typing import TYPE_CHECKING, cast

from tbhbdev import content_model, content_vet, cue_runtime, issue_import

if TYPE_CHECKING:
    from pathlib import Path

ISSUE_TYPES = issue_import.TYPES | {"Research", "Decision"}
_TYPE_NAMES = {name.lower(): name for name in ISSUE_TYPES}
_SECTION = re.compile(r"^ {0,3}## ")


def check(body: str, issue_type: str, pm_dir: Path | None) -> list[str]:
    """Return refusals for a missing ATX section or a pm schema violation."""
    variant = issue_type.lower()
    name = _TYPE_NAMES.get(variant)
    try:
        draft = content_model.normalize(
            body,
            object="issue-comment" if pm_dir is None else "issue",
            variant=variant,
            metadata={"type": name},
        )
    except ValueError as exc:
        return [str(exc)]
    blocks = cast("dict[str, content_vet.Json]", draft["blocks"])
    if not any(
        block["kind"] == "section" and _SECTION.match(cast("str", block["source"]))
        for block in blocks.values()
    ):
        return ["Issue body must contain a level-two heading starting with '## '"]
    if pm_dir is None:
        return []
    if name is None:
        return [f"Unknown issue type: {issue_type}"]
    return _check_schema(draft, pm_dir, variant)


def _check_schema(draft: content_vet.Json, pm_dir: Path, variant: str) -> list[str]:
    template = pm_dir / "templates" / f"{variant}.md"
    schemas = pm_dir / "schemas"
    variant_files = (schemas / "issue-sections.cue", schemas / f"issue.{variant}.cue")
    missing = [
        f"Missing {path}" for path in (template, *variant_files) if not path.is_file()
    ]
    if missing:
        return missing
    try:
        with as_file(files("tbhbdev").joinpath("schemas")) as shared:
            result = content_vet.check(
                draft, shared, variant_schema=variant_files, definition="#Variant"
            )
    except (cue_runtime.CueError, OSError, TimeoutExpired) as exc:
        return [str(exc)]
    return [
        content_vet.format_refusal(finding)
        for finding in result.refusals
        if not (
            finding.path.startswith("metadata")
            and "incomplete value" in finding.message
        )
    ]
