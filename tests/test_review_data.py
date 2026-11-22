"""Installed review floor resources match the current draft rules."""

import json
from importlib.resources import files

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
