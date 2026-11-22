"""Installed review floor resources match the current draft rules."""

import json
import re
from importlib.resources import files

import yaml
from jsonschema import Draft202012Validator

from tbhbdev.alpha import review


def test_template_matches_constant() -> None:
    template = (
        files("tbhbdev.alpha")
        .joinpath("review_data", "PULL_REQUEST_REVIEW_TEMPLATE.md")
        .read_text(encoding="utf-8")
    )
    assert template == review.TEMPLATE


def test_draft_schema_is_valid_and_matches_constant() -> None:
    schema = json.loads(
        files("tbhbdev.alpha")
        .joinpath("review_data", "pull-request-review-draft.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    assert schema == review.SCHEMA


def _checklist() -> str:
    return (
        files("tbhbdev.alpha")
        .joinpath("review_data", "PULL_REQUEST_REVIEW_CHECKLIST.md")
        .read_text(encoding="utf-8")
    )


def test_checklist_has_every_floor_item() -> None:
    ids = re.findall(r"^- \*\*(R\d+)\*\*", _checklist(), flags=re.MULTILINE)
    assert ids == [f"R{n}" for n in range(1, 14)]


def test_checklist_escalation_ids_are_unique_and_floor_prefixed() -> None:
    frontmatter = yaml.safe_load(_checklist().split("---\n")[1])
    ids = [rule["id"] for rule in frontmatter["escalate"]]
    assert ids
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"FE\d+", i) for i in ids)
