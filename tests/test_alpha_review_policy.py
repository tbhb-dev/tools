"""Tests for the review policy check."""

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tbhbdev.alpha import review, review_policy

if TYPE_CHECKING:
    from pathlib import Path

GOOD_TEMPLATE = (
    "## Verdict\n\n<!-- Required, at most 60 words. -->\n\n"
    "## Notes\n\n<!-- Optional. -->\n\n"
    "## Verification\n\n<!-- Required, at most 100 words. -->\n"
)


def write(root: Path, path: Path, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def rules(root: Path) -> list[str]:
    return [f.rule for f in review_policy.check(root)]


def test_a_repository_without_review_files_passes(tmp_path: Path) -> None:
    assert review_policy.check(tmp_path) == []


def test_valid_files_pass(tmp_path: Path) -> None:
    write(
        tmp_path,
        review_policy.TOML,
        'schema = 1\nreview = "required"\n[merge]\nmode = "owner"\nowner = "ops"\n',
    )
    write(tmp_path, review_policy.TEMPLATE, GOOD_TEMPLATE)
    write(tmp_path, review_policy.DRAFT_SCHEMA, '{"additionalProperties": false}')
    write(
        tmp_path,
        review_policy.CHECKLIST,
        "---\nescalate:\n  - {id: E1, class: x}\n---\n- **R100** one\n- **P1** two\n",
    )
    assert review_policy.check(tmp_path) == []


@pytest.mark.parametrize(
    "text",
    [
        "schema = ",
        "schema = 2\n",
        "schema = 1\nextra = 1\n",
        'schema = 1\nreview = "maybe"\n',
        'schema = 1\n[merge]\nmode = "owner"\n',
        'schema = 1\n[merge]\nmode = "owner"\nowner = ""\n',
    ],
)
def test_a_bad_config_is_refused(tmp_path: Path, text: str) -> None:
    write(tmp_path, review_policy.TOML, text)
    assert set(rules(tmp_path)) <= {"config-toml", "config-schema"}
    assert rules(tmp_path)


def test_a_template_must_keep_the_floor_sections_in_order(tmp_path: Path) -> None:
    swapped = (
        GOOD_TEMPLATE.replace("Verdict", "X")
        .replace("Verification", "Verdict")
        .replace("X", "Verification")
    )
    write(tmp_path, review_policy.TEMPLATE, swapped)
    assert rules(tmp_path) == ["template-floor-section"]
    write(tmp_path, review_policy.TEMPLATE, "## Verdict\n\n<!-- Required. -->\n")
    assert rules(tmp_path) == ["template-floor-section"]


def test_a_section_needs_a_guidance_comment(tmp_path: Path) -> None:
    write(
        tmp_path, review_policy.TEMPLATE, GOOD_TEMPLATE + "\n## Extra\n\nNo comment.\n"
    )
    assert rules(tmp_path) == ["template-guidance"]


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("{", "draft-schema-invalid"),
        ('{"type": 5}', "draft-schema-invalid"),
        ("{}", "draft-schema-open"),
    ],
)
def test_a_bad_draft_schema_is_refused(tmp_path: Path, text: str, rule: str) -> None:
    write(tmp_path, review_policy.DRAFT_SCHEMA, text)
    assert rules(tmp_path) == [rule]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\n: [\n---\n", ["checklist-frontmatter"]),
        ("- **R100** a\n- **R100** b\n", ["checklist-item-id"]),
        ("- **R13** a\n", ["checklist-item-id"]),
        ("---\nescalate:\n  - {id: FE1}\n---\n", ["checklist-escalate-id"]),
        (
            "---\nescalate:\n  - {id: E1}\n  - {id: E1}\n---\n",
            ["checklist-escalate-id"] * 2,
        ),
        ("---\nescalate:\n  - {class: x}\n---\n", ["checklist-escalate-id"]),
        ("---\nescalate: nope\n---\n- **E1** a\n", []),
        ("- **K1** fine\n", []),
    ],
)
def test_checklist_ids(tmp_path: Path, text: str, expected: list[str]) -> None:
    write(tmp_path, review_policy.CHECKLIST, text)
    assert rules(tmp_path) == expected


def test_an_unreadable_file_is_a_finding(tmp_path: Path) -> None:
    write(tmp_path, review_policy.TOML, "schema = 1\n")
    (tmp_path / review_policy.TOML).write_bytes(b"\xff\xfe")
    assert rules(tmp_path) == ["unreadable"]
    (tmp_path / review_policy.TOML).unlink()
    (tmp_path / review_policy.TOML).mkdir()
    assert rules(tmp_path) == ["unreadable"]


def test_main_prints_lines_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, review_policy.TOML, "schema = 2\n")
    assert review_policy.main(["check", str(tmp_path)], "p") == 1
    assert capsys.readouterr().out.startswith(
        ".github/pull_request_review.toml: config-schema: schema:"
    )


def test_main_json_and_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert review_policy.main(["check", "--json", str(tmp_path)], "p") == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "findings": []}
    write(tmp_path, review_policy.TOML, "schema = 2\n")
    assert review_policy.main(["check", "--json", str(tmp_path)], "p") == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["findings"][0].keys() == {"path", "id", "message"}


def test_main_defaults_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert review_policy.main(["check"], "p") == 0


def test_a_path_that_is_not_a_directory_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert review_policy.main(["check", str(tmp_path / "none")], "p") == 2
    assert "not a directory" in capsys.readouterr().err


def test_a_floor_that_fails_to_load_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(review_policy, "floor_required", side_effect=OSError("gone")):
        assert review_policy.main(["check", str(tmp_path)], "p") == 2
    assert "cannot load the floor: gone" in capsys.readouterr().err


def test_the_floor_requires_verdict_and_verification() -> None:
    assert review_policy.floor_required() == ["Verdict", "Verification"]


def test_review_routes_policy_to_the_check(tmp_path: Path) -> None:
    assert review.main(["policy", "check", str(tmp_path)], "tbhbdev review") == 0


def test_a_boolean_draft_schema_is_a_finding_not_a_crash(tmp_path: Path) -> None:
    for text in ("true", "false"):
        write(tmp_path, review_policy.DRAFT_SCHEMA, text)
        assert rules(tmp_path) == ["draft-schema-open"]


def test_a_required_floor_section_cannot_be_marked_optional(tmp_path: Path) -> None:
    text = GOOD_TEMPLATE.replace(
        "<!-- Required, at most 60 words. -->", "<!-- Optional. -->"
    )
    write(tmp_path, review_policy.TEMPLATE, text)
    assert rules(tmp_path) == ["template-floor-section"]


def test_a_floor_file_that_is_missing_exits_two(tmp_path: Path) -> None:
    real = review_policy.files

    def broken(package: str) -> object:
        root = real(package)
        return type(
            "R",
            (),
            {
                "joinpath": lambda _s, *parts: (
                    (_ for _ in ()).throw(FileNotFoundError(parts[-1]))
                    if parts[-1] == review_policy.CHECKLIST.name
                    else root.joinpath(*parts)
                )
            },
        )()

    with patch.object(review_policy, "files", broken):
        assert review_policy.main(["check", str(tmp_path)], "p") == 2


def test_json_output_stays_valid_on_each_exit_two_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert review_policy.main(["check", str(tmp_path / "none"), "--json"], "p") == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "not-a-directory",
        "message": f"{tmp_path / 'none'} is not a directory",
    }
    with patch.object(review_policy, "floor_required", side_effect=OSError("gone")):
        assert review_policy.main(["check", str(tmp_path), "--json"], "p") == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "floor-load",
        "message": "cannot load the floor: gone",
    }
