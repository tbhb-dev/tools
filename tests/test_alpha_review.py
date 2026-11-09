"""Tests for per-worktree review drafts and local review checks."""

import json
import re
import shutil
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator
from test_alpha_pull_request import REAL, World
from test_alpha_pull_request import world as world  # noqa: PLC0414

from tbhbdev import alpha
from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha import review

HEAD = "a1" * 20
NEXT = "B2" * 20
FRONT = f"---\npr: 7\ncommit_id: '{HEAD}'\nevent: APPROVE\n---\n"
BODY = (
    "## Verdict\n\nThe change passes.\n\n## Verification\n\n"
    "Read the diff and ran the tests. CI passed.\n"
)
FILLED = FRONT + BODY


def run_main(*argv: str) -> int:
    return review.main(list(argv), prog="tbhbdev alpha review")


def structure(text: str, number: int = 7) -> list[pr.Problem]:
    with patch.object(pr, "lint", return_value=[]):
        return review.check_text(text, number)


def head(world: World, value: str = HEAD) -> None:
    world.fake("gh", stdout=json.dumps({"headRefOid": value}))


def test_template_and_sections_agree() -> None:
    assert re.findall(r"^## (.+)$", review.TEMPLATE, re.MULTILINE) == [
        section.heading for section in review.SECTIONS
    ]
    guidance = re.findall(
        r"^## .+\n\n<!-- (Required|Optional), at most (\d+) words",
        review.TEMPLATE,
        re.MULTILINE,
    )
    assert [(g[0] == "Required", int(g[1])) for g in guidance] == [
        (section.required, section.max_words) for section in review.SECTIONS
    ]
    assert "one line per paragraph" in review.TEMPLATE
    assert "plain prose" in review.TEMPLATE
    assert "`path:line`" in review.TEMPLATE
    assert "no trailers" in review.TEMPLATE


def test_schema_is_valid_and_closed() -> None:
    Draft202012Validator.check_schema(review.SCHEMA)
    assert review.SCHEMA["additionalProperties"] is False


def test_draft_prints_path_and_writes_skeleton(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    head(world)
    assert run_main("draft", "7") == 0
    path = world.root / ".git" / "tbhb-alpha" / "review-7.md"
    assert capsys.readouterr() == (f"{path}\n", "")
    assert (
        path.read_text() == FRONT.replace("event: APPROVE", "event:") + review.TEMPLATE
    )
    assert [call["argv"] for call in world.calls("gh")] == [
        ["pr", "view", "7", "--json", "headRefOid"]
    ]
    problems = structure(path.read_text())
    assert any(line == 4 and "event" in why for line, why in problems)
    assert any("HTML comment" in why for _, why in problems)


def test_existing_draft_is_unchanged_without_fetch(world: World) -> None:
    path = review.draft_path(7)
    path.parent.mkdir()
    path.write_bytes(b"untouched\r\n")
    assert review.draft(7, force=False) == path
    assert path.read_bytes() == b"untouched\r\n"
    assert world.calls("gh") == []


def test_force_refreshes_only_commit_id(world: World) -> None:
    head(world, NEXT)
    path = review.draft_path(7)
    path.parent.mkdir()
    original = FILLED + "\ncommit_id: body text\n"
    path.write_text(original)
    assert review.draft(7, force=True) == path
    assert path.read_text() == original.replace(HEAD, NEXT)
    assert len(world.calls("gh")) == 1


def test_force_can_create_a_draft_and_prs_have_separate_files(world: World) -> None:
    head(world)
    first = review.draft(7, force=True)
    second = review.draft(8, force=False)
    assert first != second
    assert first.read_text().startswith("---\npr: 7\n")
    assert second.read_text().startswith("---\npr: 8\n")


def test_subdirectory_and_linked_worktrees_have_their_own_state(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    head(world)
    main_path = review.draft(7, force=False)
    linked = world.root.parent / "linked"
    world.git("worktree", "add", "-q", "-b", "topic", str(linked))
    sub = linked / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    path = review.draft(7, force=False)
    assert (
        path
        == world.root / ".git" / "worktrees" / "linked" / "tbhb-alpha" / "review-7.md"
    )
    assert path != main_path
    assert path.read_text() == main_path.read_text()


def test_outside_git_fails(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(world.root.parent)
    with pytest.raises(SystemExit, match=r"^review drafts require a git worktree$"):
        review.draft_path(7)
    assert world.calls("gh") == []


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr", "message"),
    [
        (1, "", "bad\n request", "gh pr view failed: bad request"),
        (0, "{", "", "gh pr view did not return a 40-hex headRefOid"),
        (0, "[]", "", "gh pr view did not return a 40-hex headRefOid"),
        (0, "{}", "", "gh pr view did not return a 40-hex headRefOid"),
        (0, '{"headRefOid": 1}', "", "gh pr view did not return a 40-hex headRefOid"),
        (
            0,
            '{"headRefOid": "abc"}',
            "",
            "gh pr view did not return a 40-hex headRefOid",
        ),
    ],
)
def test_bad_gh_leaves_draft_unchanged(
    world: World, rc: int, stdout: str, stderr: str, message: str
) -> None:
    world.fake("gh", rc=rc, stdout=stdout, stderr=stderr)
    path = review.draft_path(7)
    path.parent.mkdir()
    path.write_text(FILLED)
    with pytest.raises(SystemExit, match=f"^{message}$"):
        review.draft(7, force=True)
    assert path.read_text() == FILLED


@pytest.mark.parametrize("text", [BODY, "---\n---\n" + BODY, "---\n{}\n---\n" + BODY])
def test_missing_or_empty_frontmatter_stops_before_body_and_lint(text: str) -> None:
    with patch.object(pr, "lint") as lint:
        assert review.check_text(text, 7) == [
            (1, "review frontmatter is required and must not be empty")
        ]
        lint.assert_not_called()


@pytest.mark.parametrize("text", ["---\n", "---\n[\n---\n"])
def test_bad_yaml_stops_before_lint(text: str) -> None:
    with patch.object(pr, "lint") as lint:
        found = review.check_text(text, 7)
        assert found[0][0] == 1
        assert found[0][1].startswith("frontmatter: ")
        lint.assert_not_called()


@pytest.mark.parametrize(
    ("old", "new", "line", "message"),
    [
        ("pr: 7", "pr: 8", 2, "7 was expected"),
        ("pr: 7", "pr: 0", 2, "minimum"),
        ("pr: 7", "pr: true", 2, "integer"),
        ("pr: 7", "pr: '7'", 2, "integer"),
        ("pr: 7\n", "", 1, "required property"),
        (f"commit_id: '{HEAD}'", "commit_id: 123", 3, "string"),
        (f"commit_id: '{HEAD}'", "commit_id: 'abc'", 3, "does not match"),
        (f"commit_id: '{HEAD}'\n", "", 1, "required property"),
        ("event: APPROVE", "event:", 4, "not one of"),
        ("event: APPROVE", "event: approve", 4, "not one of"),
        ("event: APPROVE\n", "", 1, "required property"),
        ("event: APPROVE", "event: APPROVE\nextra: value", 1, "Additional properties"),
    ],
)
def test_schema_errors_point_to_the_key(
    old: str, new: str, line: int, message: str
) -> None:
    assert any(
        n == line and message in why for n, why in structure(FILLED.replace(old, new))
    )


@pytest.mark.parametrize("meta", ["[]", "false", "3", "text"])
def test_non_mapping_frontmatter_fails(meta: str) -> None:
    assert "not of type 'object'" in structure(f"---\n{meta}\n---\n{BODY}")[0][1]


@pytest.mark.parametrize("event", ["APPROVE", "COMMENT", "REQUEST_CHANGES"])
def test_events_and_uppercase_hex_pass(event: str) -> None:
    body = BODY.replace(
        "## Verification",
        "## Findings\n\n- `src/file.py:3` must handle empty input.\n\n## Verification",
    )
    assert structure((FRONT + body).replace("APPROVE", event).replace(HEAD, NEXT)) == []


@pytest.mark.parametrize(
    "body", [BODY, BODY.replace("## Verification", "## Findings\n\n## Verification")]
)
def test_request_changes_needs_findings(body: str) -> None:
    assert (6, "REQUEST_CHANGES requires non-empty Findings") in structure(
        FRONT.replace("APPROVE", "REQUEST_CHANGES") + body
    )


def test_comments_do_not_count_as_findings() -> None:
    text = FRONT.replace("APPROVE", "REQUEST_CHANGES") + BODY.replace(
        "## Verification", "## Findings\n\n<!-- TODO -->\n\n## Verification"
    )
    assert (6, "REQUEST_CHANGES requires non-empty Findings") in structure(text)


@pytest.mark.parametrize(
    ("body", "line", "message"),
    [
        ("# Title\n\n" + BODY, 6, "a review must not contain an H1 title"),
        ("stray\n\n" + BODY, 6, "text before the first section"),
        (BODY.replace("## Verdict", "## Other"), 6, "unknown section"),
        (
            BODY.replace("## Verification", "## Verdict"),
            10,
            "duplicated or out of order",
        ),
        (
            BODY.replace("## Verdict\n\nThe change passes.\n\n", ""),
            6,
            "missing required section 'Verdict'",
        ),
        ("## Verdict\n\nFine.\n", 6, "missing required section 'Verification'"),
        (BODY.replace("The change passes.", ""), 6, "section 'Verdict' is empty"),
        (BODY.replace("The change passes.", "one\ntwo"), 8, "hard-wrapped"),
        (BODY + "\nGenerated-by: LLM test\n", 14, "the script writes Generated-by"),
        (BODY + "\nSigned-off-by: test\n", 14, "Signed-off-by and Co-authored-by"),
        (BODY.replace("The change passes.", "See #7."), 8, "#7 links"),
        (BODY + "\n```\n", 14, "code fence is never closed"),
    ],
)
def test_body_checks_are_reused(body: str, line: int, message: str) -> None:
    assert any(n == line and message in why for n, why in structure(FRONT + body))


@pytest.mark.parametrize("section", review.SECTIONS)
def test_each_section_has_its_word_limit(section: pr.Section) -> None:
    body = "\n\n".join(f"## {s.heading}\n\nFine." for s in review.SECTIONS) + "\n"
    assert structure(FRONT + body) == []
    assert section.max_words is not None
    text = body.replace(
        f"## {section.heading}\n\nFine.",
        f"## {section.heading}\n\n" + "word " * (section.max_words + 1),
    )
    assert any(
        f"limit {section.max_words}" in why for _, why in structure(FRONT + text)
    )
    boundary = body.replace(
        f"## {section.heading}\n\nFine.",
        f"## {section.heading}\n\n" + "word " * section.max_words,
    )
    assert structure(FRONT + boundary) == []


@pytest.mark.parametrize(
    "text", [BODY, "---\npr: 7\n---\n" + BODY, "---\n[]\n---\n" + BODY]
)
def test_refresh_needs_a_commit_line(text: str) -> None:
    with pytest.raises(
        SystemExit, match=r"^frontmatter needs a commit_id field to refresh$"
    ):
        review.refresh(text, HEAD)


def test_refresh_reports_bad_yaml() -> None:
    with pytest.raises(SystemExit, match=r"^frontmatter: unterminated frontmatter$"):
        review.refresh("---\n", HEAD)


def test_check_prints_problems_and_never_reads_gh(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = review.draft_path(7)
    path.parent.mkdir()
    path.write_text(FILLED.replace("event: APPROVE", "event:"))
    assert run_main("check", "7") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(f"{path}:4: frontmatter $.event:")
    assert world.calls("gh") == []
    path.write_text(FILLED)
    assert run_main("check", "7") == 0
    assert capsys.readouterr() == ("", "")
    assert world.calls("gh") == []


@pytest.mark.parametrize("kind", ["missing", "directory", "bytes"])
def test_check_reports_unreadable_drafts(
    world: World, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    path = review.draft_path(7)
    if kind == "directory":
        path.mkdir(parents=True)
    if kind == "bytes":
        path.parent.mkdir()
        path.write_bytes(b"\xff")
    assert run_main("check", "7") == 1
    assert capsys.readouterr().err.startswith(f"{path}:1: cannot read draft:")
    assert world.calls("gh") == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["post", "7"],
        ["dismiss", "7"],
        ["draft"],
        ["check", "0"],
        ["draft", "-1"],
        ["check", "x"],
        ["check", "7", "--force"],
    ],
)
def test_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main(*argv)
    assert info.value.code == 2


def test_help_uses_the_program_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("--help")
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: tbhbdev alpha review ")
    assert "Draft and check a pull request review in per-worktree git state." in out


def test_alpha_dispatch(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    head(world)
    assert alpha.main(["review", "draft", "7"], "tbhbdev alpha") == 0
    assert capsys.readouterr().out == f"{review.draft_path(7)}\n"
    assert "\n  review " in alpha.USAGE


def test_lint_uses_review_path_and_drops_only_missing_h1(world: World) -> None:
    missing = "[MD041] First line in file should be a level 1 heading"
    world.fake(
        "rumdl",
        rc=1,
        stdout=(
            f"draft:1:1: {missing}\n"
            "draft:1:3: [MD009] Trailing spaces\n"
            f"draft:3:1: {missing}\n"
        ),
    )
    world.fake(
        "vale",
        rc=1,
        stdout=json.dumps(
            {
                "draft": [
                    {"Line": 1, "Check": "project.HeadingTitleCase", "Message": "case"}
                ]
            }
        ),
    )
    assert review.check_text(FILLED, 7) == [
        (6, "rumdl: [MD009] Trailing spaces"),
        (6, "vale project.HeadingTitleCase: case"),
        (8, f"rumdl: {missing}"),
    ]
    for name in ("rumdl", "vale", "cspell"):
        (call,) = world.calls(name)
        assert cast("list[str]", call["argv"])[-1] == str(review.LINT_PATH)
        # The existing fake records only the PR draft; the review file is separate.
        assert call["draft"] is None


@pytest.mark.skipif(
    not (
        (REAL / ".vale" / "Google").is_dir()
        and all(shutil.which(name) for name in ("vale", "cspell", "rumdl"))
    ),
    reason="linters or their configs are missing",
)
@pytest.mark.parametrize("prefix", ["", "\n"])
def test_filled_template_with_real_linters(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    monkeypatch.chdir(REAL)
    replacements = [
        "The change passes.",
        "- `src/file.py:3` must handle empty input.",
        "Add an example for empty input.",
        "Read the diff and ran the tests. CI passed.",
    ]
    filled = re.sub(r"<!--.*?-->\n*", "", review.TEMPLATE, count=1, flags=re.DOTALL)
    for replacement in replacements:
        filled = re.sub(
            r"<!--.*?-->",
            lambda _match, replacement=replacement: replacement,
            filled,
            count=1,
            flags=re.DOTALL,
        )
    assert review.check_text(FRONT + prefix + filled, 7) == []


@pytest.mark.parametrize(
    "front",
    [
        (
            f"---\n'pr': 7\n\"commit_id\": '{HEAD}' # keep this comment\n"
            "event: COMMENT\n---\n"
        ),
        f"---\n{{pr: 7, commit_id: '{HEAD}', event: COMMENT}}\n---\n",
        f"---\npr: 7\ncommit_id: >-\n  {HEAD}\nevent: COMMENT\n---\n",
    ],
)
def test_refresh_preserves_yaml_layout_and_body(front: str) -> None:
    text = front + BODY + "\ncommit_id: body text\n"
    changed = review.refresh(text, NEXT)
    data, body = pr.parse_frontmatter(changed)
    assert data == {"pr": 7, "commit_id": NEXT, "event": "COMMENT"}
    assert body == BODY + "\ncommit_id: body text\n"
    if ">-" not in front:
        assert changed == text.replace(HEAD, NEXT)


def test_force_preserves_crlf_body_bytes(world: World) -> None:
    head(world, NEXT)
    path = review.draft_path(7)
    path.parent.mkdir()
    text = FILLED.replace("\n", "\r\n")
    path.write_bytes(text.encode())
    assert review.draft(7, force=True) == path
    assert path.read_bytes() == text.replace(HEAD, NEXT).encode()


@pytest.mark.parametrize("value", ["a" * 39, "a" * 41, "g" * 40, HEAD + "\n"])
def test_commit_is_exactly_40_hex_characters(value: str) -> None:
    encoded = json.dumps(value)
    text = FILLED.replace(f"'{HEAD}'", encoded)
    assert any(line == 3 and "commit_id" in why for line, why in structure(text))


def test_a_fenced_h1_is_code() -> None:
    assert structure(FILLED + "\n```text\n# An example\n```\n") == []


def test_dispatch_forwards_arguments_program_and_status() -> None:
    with patch.object(review, "main", return_value=3) as run:
        assert alpha.main(["review", "check", "7"], "p") == 3
        run.assert_called_once_with(["check", "7"], prog="p review")


def test_explicit_empty_sections_do_not_load_the_repository_scheme() -> None:
    anchor = pr.Line(9, "", "", code=False)
    with patch.object(pr, "load_sections") as load:
        assert pr.body_problems([], anchor, []) == []
        load.assert_not_called()


def test_lint_can_keep_the_default_title_alert(world: World) -> None:
    world.fake(
        "vale",
        rc=1,
        stdout=json.dumps(
            {
                "draft": [
                    {"Line": 1, "Check": "project.HeadingTitleCase", "Message": "case"}
                ]
            }
        ),
    )
    assert pr.lint(["# title"], 12, drop=frozenset()) == [
        (12, "vale project.HeadingTitleCase: case")
    ]


@pytest.mark.parametrize("path", [Path("draft.md"), Path(".github/reviews/draft.md")])
def test_lint_creates_the_requested_relative_path(world: World, path: Path) -> None:
    assert pr.lint(["## Verdict", "", "Fine."], 6, path=path) == []
    for name in ("vale", "cspell", "rumdl"):
        (call,) = world.calls(name)
        assert cast("list[str]", call["argv"])[-1] == str(path)


@pytest.mark.parametrize("body", ["", "\n", " \n\t\n"])
def test_empty_body_reports_required_sections(body: str) -> None:
    assert structure(FRONT + body) == [
        (6, "missing required section 'Verdict'"),
        (6, "missing required section 'Verification'"),
    ]


def test_schema_key_search_stays_in_the_frontmatter() -> None:
    text = FILLED.replace("event: APPROVE", "'event': invalid") + "\nevent: body text\n"
    assert structure(text) == [
        (
            1,
            (
                "frontmatter $.event: 'invalid' is not one of "
                "['APPROVE', 'REQUEST_CHANGES', 'COMMENT']"
            ),
        )
    ]


def test_h1_diagnostic_is_specific_to_reviews() -> None:
    assert structure(FRONT + "# Title\n\n" + BODY) == [
        (6, "a review must not contain an H1 title"),
        (6, "text before the first section; put it under a heading"),
    ]


def test_request_changes_keeps_other_problems() -> None:
    text = FILLED.replace("APPROVE", "REQUEST_CHANGES").replace(HEAD, "abc")
    found = structure(text)
    assert any(line == 3 and "commit_id" in why for line, why in found)
    assert (6, "REQUEST_CHANGES requires non-empty Findings") in found


def test_force_flag_refreshes_the_draft(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    head(world, NEXT)
    path = review.draft_path(7)
    path.parent.mkdir()
    path.write_text(FILLED)
    assert run_main("draft", "7", "--force") == 0
    assert capsys.readouterr() == (f"{path}\n", "")
    assert path.read_text() == FILLED.replace(HEAD, NEXT)


def test_pr_one_is_allowed(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    head(world)
    assert run_main("draft", "1") == 0
    path = review.draft_path(1)
    assert capsys.readouterr() == (f"{path}\n", "")
    assert path.read_text().startswith("---\npr: 1\n")


@pytest.mark.parametrize("number", ["0", "-1"])
def test_number_error_is_explicit(
    number: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("check", number)
    assert info.value.code == 2
    assert capsys.readouterr().err.endswith(
        "tbhbdev alpha review: error: PR number must be at least 1\n"
    )


@pytest.mark.parametrize("value", ["", " ", " null"])
def test_refresh_fills_an_empty_commit_value(value: str) -> None:
    text = FILLED.replace(f" '{HEAD}'", value)
    changed = review.refresh(text, NEXT)
    data, body = pr.parse_frontmatter(changed)
    assert data == {"pr": 7, "commit_id": NEXT, "event": "APPROVE"}
    assert body == BODY


def test_empty_body_skips_external_linters() -> None:
    with patch.object(pr, "lint") as lint:
        assert review.check_text(FRONT + " \n", 7) == [
            (6, "missing required section 'Verdict'"),
            (6, "missing required section 'Verification'"),
        ]
        lint.assert_not_called()
