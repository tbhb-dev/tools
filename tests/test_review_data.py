"""Installed review floor resources match the current draft and verdict rules."""

import json
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator

from tbhbdev.alpha import review

HEAD = "a1" * 20
TECH_DEBT = {
    "class": "TECH-DEBT",
    "location": "src/tool.py:1",
    "summary": "Handle optional input in a follow-up.",
}
BLOCK = {**TECH_DEBT, "class": "BLOCK", "summary": "Normal input fails."}
REVIEWER = {
    "provider": "openai",
    "harness": "codex",
    "model": "gpt-6.1-sol",
    "run": "review-floor",
}


def test_template_matches_constant() -> None:
    template = (
        files("tbhbdev.alpha")
        .joinpath("review_data", "PULL_REQUEST_REVIEW_TEMPLATE.md")
        .read_text(encoding="utf-8")
    )
    assert template == review.TEMPLATE


@pytest.mark.parametrize("kind", ["draft", "verdict"])
def test_schemas_are_valid(kind: str) -> None:
    schema = json.loads(
        files("tbhbdev.alpha")
        .joinpath("review_data", f"pull-request-review-{kind}.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)


def test_draft_schema_matches_constant() -> None:
    schema = json.loads(
        files("tbhbdev.alpha")
        .joinpath("review_data", "pull-request-review-draft.schema.json")
        .read_text(encoding="utf-8")
    )
    assert schema == review.SCHEMA


@pytest.mark.parametrize(
    ("changes", "valid"),
    [
        pytest.param({}, True, id="go-with-tech-debt"),
        pytest.param(
            {"verdict": "HOLD", "findings": [BLOCK]}, True, id="hold-with-block"
        ),
        pytest.param({"verdict": "HOLD"}, False, id="hold-without-block"),
        pytest.param({"findings": [BLOCK]}, False, id="go-with-block"),
        pytest.param({"commit_id": "invalid"}, False, id="bad-commit-id"),
        pytest.param(
            {"reviewer": {**REVIEWER, "provider": "unknown"}},
            False,
            id="unknown-provider",
        ),
        pytest.param({"extra": True}, False, id="extra-field"),
    ],
)
def test_verdict_cases(changes: dict[str, object], valid: bool) -> None:
    schema = json.loads(
        files("tbhbdev.alpha")
        .joinpath("review_data", "pull-request-review-verdict.schema.json")
        .read_text(encoding="utf-8")
    )
    verdict = {
        "pr": 7,
        "commit_id": HEAD,
        "verdict": "GO",
        "reviewer": REVIEWER,
        "builders": [{"commit": HEAD, "provider": "google"}],
        "findings": [TECH_DEBT],
        **changes,
    }
    assert Draft202012Validator(schema).is_valid(verdict) is valid
