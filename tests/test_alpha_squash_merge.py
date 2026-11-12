# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for tbhbdev alpha merge, the squash-merge command."""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fake_tool import link_git, pythonpath, write_fake, write_private_config
from hypothesis import example, given
from hypothesis import strategies as st

from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha import squash_merge as sm

SRC = Path(sm.__file__).parents[2]

FIXTURE = (
    Path(__file__).parent / "testdata" / "alpha" / "pull_request" / "passing-draft.md"
)
HARNESS_ENV = {"CLAUDE_CODE_SESSION_ID": "raw-session"}
GEN = (
    "Generated-by: LLM harness=claude-code:2.1.0;model=anthropic:m1;"
    "session=sha-256-64:aaaaaaaaaaaaaaaa"
)
TARGET = "tbhb-dev/public-target"
ASSIST = "Assisted-by: LLM harness=codex;model=openai:gpt-6.1-sol"

GH = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(here, "gh.spec")))
repo = sys.argv[1] == "repo"
# A read with no repository argument names the working directory's repository.
if repo:
    kind = "cwd" if sys.argv[2].startswith("-") else "repo"
else:
    kind = sys.argv[2] if sys.argv[1] == "pr" else "?"
# The visibility read has its own log, so it never shifts the other gh calls.
with open(os.path.join(here, "gh.repo.log" if repo else "gh.log"), "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
sys.stdout.write(spec[kind + "_out"])
sys.stderr.write(spec[kind + "_err"])
sys.exit(spec[kind + "_rc"])
"""
GITLEAKS = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
spec = os.path.join(here, "gitleaks.spec")
rc, out = json.load(open(spec)) if os.path.exists(spec) else (0, "[]")
with open(os.path.join(here, "gitleaks.log"), "a") as log:
    entry = {{"cwd": os.getcwd(), "stdin": sys.stdin.read(), "argv": sys.argv[1:]}}
    log.write(json.dumps(entry) + "\\n")
sys.stdout.write(out)
sys.exit(rc)
"""
COG = """import sys
text = sys.stdin.read()
if "BAD" in text:
    sys.stderr.write("not conventional")
    sys.exit(1)
"""


@dataclass
class Tools:
    """Fake gh and cog on an isolated PATH."""

    bin: Path

    def gh(self, **spec: object) -> None:
        full: dict[str, object] = {
            "view_rc": 0,
            "view_out": "",
            "view_err": "",
            "merge_rc": 0,
            "merge_out": "merged\n",
            "merge_err": "",
            "repo_rc": 0,
            "repo_out": "PUBLIC\\n",
            "repo_err": "",
            "cwd_rc": 0,
            "cwd_out": "PRIVATE\\n",
            "cwd_err": "",
        }
        (self.bin / "gh.spec").write_text(json.dumps(full | spec))

    def view(self, **fields: object) -> None:
        data: dict[str, object] = {
            "title": "feat(cli): add the stamp subcommand",
            "body": FIXTURE.read_text().split("\n# ", 1)[1].split("\n", 1)[1],
            "headRefOid": "abc123",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [],
            "url": f"https://github.com/{TARGET}/pull/12",
        }
        self.gh(view_out=json.dumps(data | fields))

    def scanned(self) -> list[dict[str, object]]:
        log = self.bin / "gitleaks.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def calls(self) -> list[dict[str, object]]:
        log = self.bin / "gh.log"
        return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tools:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_dir = tmp_path / "git"
    link_git(git_dir)
    for name, body in (("gh", GH), ("cog", COG), ("gitleaks", GITLEAKS)):
        write_fake(bin_dir / name, body.format())
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    prefixes = ("CLAUDE", "CODEX", "ANTIGRAVITY", "GIT_")
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(prefixes)]))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{git_dir}")
    # The scan runs from the repository root, which outside a git worktree is
    # the current directory, and needs the committed and private configs.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitleaks.toml").write_text("")
    monkeypatch.chdir(repo)
    write_private_config(tmp_path)
    made = Tools(bin_dir)
    made.view()
    return made


def message_of(body: str, title: str = "fix: x", number: int = 7) -> str:
    return sm.build_message(title, number, body).text()


def read_back(message: str) -> str:
    done = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


# ------------------------------------------------------------------ wrapping

WORD = st.text(
    st.characters(categories=("L", "N", "P", "S"), exclude_characters="`"),
    min_size=1,
    max_size=90,
)
SPAN = st.builds(
    "`{}`".format,
    st.text("ab -=()", min_size=1, max_size=30).filter(
        lambda text: text.strip() == text and text
    ),
)
TEXT = st.lists(st.one_of(WORD, SPAN), min_size=1, max_size=60).map(" ".join)
PREFIX = st.sampled_from([("", ""), ("- ", "  "), ("12. ", "    "), ("  * ", "    ")])


@given(text=TEXT, prefix=PREFIX)
def test_wrap_keeps_every_token_in_order_within_the_limit(
    text: str, prefix: tuple[str, str]
) -> None:
    lines = sm.wrap(text, *prefix)
    tokens = [match[0] for match in sm.TOKEN.finditer(text)]
    bare = [
        line.removeprefix(prefix[0] if i == 0 else prefix[1])
        for i, line in enumerate(lines)
    ]
    assert [m[0] for m in sm.TOKEN.finditer("\n".join(bare))] == tokens
    for line in lines:
        assert len(line) <= sm.WIDTH or len(sm.TOKEN.findall(line)) == 1
    assert lines[0].startswith(prefix[0])
    assert all(line.startswith(prefix[1]) for line in lines[1:])


def test_wrap_never_splits_a_long_token() -> None:
    url = "https://example.com/" + "a" * 100
    assert sm.wrap(f"see {url} now", "", "") == ["see", url, "now"]


def test_wrap_keeps_a_code_span_whole() -> None:
    text = "x" * 60 + " `a long span here` tail"
    assert sm.wrap(text, "", "") == ["x" * 60, "`a long span here` tail"]


# ------------------------------------------------------------------- render


def test_sections_become_plain_headings_with_blank_lines_between() -> None:
    body = "## Why\n\nBecause.\n\nTwice.\n\n## Risk\n\nNone here.\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nBecause.\n\nTwice.\n\nRisk\nNone here.\n"
    )


def test_comments_and_empty_sections_vanish() -> None:
    body = (
        "<!-- guidance\nacross lines -->\n\n## Why\n\n<!-- Required -->\n\n"
        "## Decisions\n\n<!-- Optional -->\n\n## Risk\n\nReal text. <!-- x -->\n"
    )
    assert message_of(body) == "fix: x (#7)\n\nRisk\nReal text.\n"


def test_preamble_text_comes_first() -> None:
    assert (
        message_of("Lead in.\n\n## Why\n\nw\n") == "fix: x (#7)\n\nLead in.\n\nWhy\nw\n"
    )


def test_prose_wraps_and_joins_soft_breaks() -> None:
    words = " ".join(["word"] * 40)
    out = message_of(f"## Why\n\n{words[:50]}\n{words[50:]}\n").splitlines()
    assert max(map(len, out)) <= sm.WIDTH
    assert " ".join(out[3:]).split() == ["word"] * 40


def test_list_items_keep_markers_with_a_hanging_indent() -> None:
    items = "- " + "alpha " * 20 + "\n  more\n2. second\n  * nested\n"
    out = message_of(f"## Verification\n\n{items}").splitlines()
    assert out[3].startswith("- alpha")
    assert out[4].startswith("  alpha") or out[4].startswith("  more")
    assert any(line.startswith("2. second") for line in out)
    assert "  * nested" in out
    assert all(len(line) <= sm.WIDTH for line in out)


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ("- a\n- b\n", "- a\n- b\n"),
        ("Intro\n\n- a\n- b\n\nAfter\n", "Intro\n\n- a\n- b\n\nAfter\n"),
        ("Para\n- a\n- b\n", "Para\n\n- a\n- b\n"),
        ("- a\n\n- b\n", "- a\n\n- b\n"),
        ("- a\n```\nx\n```\n- b\n", "- a\n\n    x\n\n- b\n"),
    ],
)
def test_a_tight_list_stays_one_block(body: str, want: str) -> None:
    assert sm.build_message("fix: x", 7, body).body == want


def test_fenced_code_is_indented_not_wrapped() -> None:
    long = "$ " + "x" * 100
    body = f"## Verification\n\nRan:\n\n```console\n{long}\n\nok\n```\n\nDone.\n"
    assert message_of(body) == (
        f"fix: x (#7)\n\nVerification\nRan:\n\n    {long}\n\n    ok\n\nDone.\n"
    )


def test_a_longer_fence_ignores_a_shorter_inner_one() -> None:
    body = "## Why\n\n````md\n```\ninner\n```\n````\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n    ```\n    inner\n    ```\n"


def test_other_fence_characters_do_not_close() -> None:
    body = "## Why\n\n~~~\n```\n~~~\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n    ```\n"


LONG_CELL = "the check " + "ran again " * 10 + "and passed"


def test_a_table_keeps_its_rows_unwrapped() -> None:
    body = (
        "## Verification\n\nChecks:\n"
        "| Check | Result |\n|---|---|\n"
        f"| `uv run pytest` | {LONG_CELL} |\n  | b | **c** |  \n"
        "Then prose.\n\n| x |\n\n| y |\n"
    )
    assert message_of(body) == (
        "fix: x (#7)\n\nVerification\nChecks:\n\n"
        f"| Check | Result |\n|---|---|\n| `uv run pytest` | {LONG_CELL} |\n"
        "| b | **c** |\n\nThen prose.\n\n| x |\n\n| y |\n"
    )


def test_a_table_ends_a_list_and_a_fence_keeps_its_bars() -> None:
    body = "## Why\n\nLead.\n\n- a\n| x |\n|---|\n| z |\n- b\n\n```\n| y |\n```\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nLead.\n\n- a\n\n| x |\n|---|\n| z |\n\n- b\n\n    | y |\n"
    )


def test_a_delimiter_row_needs_no_outer_bars() -> None:
    body = "## Why\n\n| a | b |\n:--- | ---:\n| 1 | 2 |\n"
    assert message_of(body) == "fix: x (#7)\n\nWhy\n| a | b |\n:--- | ---:\n| 1 | 2 |\n"


def test_a_table_may_end_on_its_delimiter_row() -> None:
    lines = [
        pr.Line(1, "| a |", "| a |", code=False),
        pr.Line(2, "|-|", "|-|", code=False),
    ]
    assert sm.render(lines, {}) == ["| a |\n|-|"]


def test_a_bar_line_without_a_delimiter_row_is_prose() -> None:
    body = f"## Why\n\n- a\n| {LONG_CELL}\n|\n| x |\n\n| y |\n|---|\nz\n| w |\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\n"
        "- a | the check ran again ran again ran again ran again ran again ran\n"
        "  again ran again ran again ran again ran again and passed | | x |\n\n"
        "| y |\n|---|\n\nz | w |\n"
    )


def test_links_become_numbered_references() -> None:
    body = (
        "## Why\n\nSee [the issue](https://x.test/1) and "
        "[the spec](https://x.test/2) and [again](https://x.test/1).\n"
        "Bare https://x.test/3 and <https://x.test/4> and "
        "[https://x.test/5](https://x.test/5).\n"
    )
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nSee the issue [1] and the spec [2] and again [1]. "
        "Bare https://x.test/3\nand https://x.test/4 and https://x.test/5.\n\n"
        "[1]: https://x.test/1\n[2]: https://x.test/2\n"
    )


def test_emphasis_goes_and_inline_code_stays() -> None:
    body = "## Why\n\nA **strong** and *soft* word, `*raw* [a](b)` and 2 * 3 * 4.\n"
    assert message_of(body) == (
        "fix: x (#7)\n\nWhy\nA strong and soft word, `*raw* [a](b)` and 2 * 3 * 4.\n"
    )


def test_a_nested_heading_is_kept_as_text() -> None:
    assert message_of("## Why\n\n### Sub\n\nx\n") == "fix: x (#7)\n\nWhy\nSub\n\nx\n"


def test_an_empty_body_is_a_subject_only() -> None:
    assert message_of("") == "fix: x (#7)\n"
    assert message_of("<!-- only -->\n") == "fix: x (#7)\n"


@given(text=TEXT)
@example(
    text="|0 00000000000 00000000000 00000000000 000000000000000 0000000000000000 0"
)
def test_rendered_prose_is_within_the_limit(text: str) -> None:
    text = text.replace("\\", "").replace("*", "").replace("_", "").replace("[", "")
    out = sm.render([pr.Line(1, text, text, code=False)], {})
    for line in "\n".join(out).splitlines():
        assert len(line) <= sm.WIDTH or len(sm.TOKEN.findall(line)) == 1


# ------------------------------------------------------------------ trailers


def test_final_trailer_block_is_kept_verbatim_and_last() -> None:
    body = f"## Why\n\nw\n\n{GEN}\n{ASSIST}\n"
    message = message_of(body)
    assert message.endswith(f"\n\n{GEN}\n{ASSIST}\n")
    assert read_back(message) == f"{GEN}\n{ASSIST}\n"


def test_trailers_are_never_wrapped() -> None:
    long = GEN + ";surface=" + "x" * 90
    message = message_of(f"## Why\n\nw\n\n{long}\n")
    assert message.splitlines()[-1] == long


def test_trailers_alone_after_the_subject() -> None:
    assert message_of(f"{GEN}\n") == f"fix: x (#7)\n\n{GEN}\n"


def test_a_trailer_inside_a_paragraph_is_not_a_trailer_block() -> None:
    with pytest.raises(SystemExit, match="remove this line"):
        message_of(f"## Why\n\nw\n{GEN}\n\nmore\n")


def test_a_trailer_in_the_middle_is_refused() -> None:
    with pytest.raises(SystemExit, match="remove this line"):
        message_of(f"## Why\n\n{GEN}\n\nw\n")


def test_an_invalid_trailer_value_is_refused() -> None:
    with pytest.raises(SystemExit, match="invalid trailer"):
        message_of("## Why\n\nw\n\nGenerated-by: nonsense\n")


@pytest.mark.parametrize("key", ["Signed-off-by", "Co-authored-by", "signed-off-by"])
def test_human_trailers_in_the_body_are_refused(key: str) -> None:
    with pytest.raises(SystemExit, match="do not belong in the description"):
        message_of(f"## Why\n\nw\n\n{key}: A <a@b.c>\n")


def test_a_trailer_git_cannot_read_back_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    done = subprocess.CompletedProcess([], 0, stdout="other\n", stderr="")

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return done

    def same(name: str) -> str:
        return name

    monkeypatch.setattr(pr, "run", fake_run)
    monkeypatch.setattr(pr, "find_tool", same)
    with pytest.raises(SystemExit, match="did not survive"):
        sm.check_round_trip("m", [GEN])


# ------------------------------------------------------------------- subject


@pytest.mark.usefixtures("tools")
def test_subject_gets_the_pr_number() -> None:
    assert (
        sm.build_message("feat(cli): add x", 12, "").subject == "feat(cli): add x (#12)"
    )


@pytest.mark.usefixtures("tools")
def test_subject_over_72_with_the_suffix_fails() -> None:
    title = "feat: " + "a" * (72 - len("feat: ") - len(" (#5)") + 1)
    assert len(title) + len(" (#5)") == sm.WIDTH + 1
    with pytest.raises(SystemExit, match="characters; the limit is 72"):
        sm.build_message(title, 5, "")


@pytest.mark.usefixtures("tools")
def test_subject_of_exactly_72_passes() -> None:
    title = "feat: " + "a" * (72 - len("feat: ") - len(" (#5)"))
    assert len(sm.build_message(title, 5, "").subject) == sm.WIDTH


@pytest.mark.usefixtures("tools")
def test_subject_that_fails_cog_fails() -> None:
    with pytest.raises(SystemExit, match="cog verify"):
        sm.build_message("BAD title", 5, "")


def test_an_unclosed_fence_is_refused() -> None:
    with pytest.raises(SystemExit, match="never closed"):
        message_of("## Why\n\n```\ncode\n")


# ------------------------------------------------------------- fixture body


def test_the_passing_draft_becomes_a_clean_message(tools: Tools) -> None:
    body = json.loads((tools.bin / "gh.spec").read_text())["view_out"]
    request = sm.fetch(1)
    assert json.loads(body)["body"] == request.body
    message = sm.build_message(request.title, 12, request.body).text()
    assert message == (
        "feat(cli): add the stamp subcommand (#12)\n\n"
        "Why\n"
        "Running `tool stamp` on a tree with a read-only file stops partway. The\n"
        "command leaves some files stamped and others not, and prints nothing\n"
        "that says which. See the issue [1].\n\n"
        "Behavior change\n"
        "The command now checks every target before it writes one. A read-only\n"
        "file fails the whole run with an exit code of 1 and a list of the\n"
        "blocked paths. A run that passes the check stamps every file.\n\n"
        "Verification\n"
        "Ran the new unit tests, then stamped a tree with one read-only file:\n\n"
        "    $ go test ./internal/stamp/...\n"
        "    ok  \tgithub.com/tbhb-dev/tools/internal/stamp\t0.412s\n"
        "    $ tool stamp ./fixture\n"
        "    error: 1 file is read-only: fixture/a.txt\n\n"
        "No file in the tree changed after the failure.\n\n"
        "[1]: https://github.com/tbhb-dev/tools/issues/12\n"
    )


# ------------------------------------------------------------------- the CLI


def run_main(*argv: str) -> tuple[int | str | None, str]:
    try:
        return sm.main(list(argv)), ""
    except SystemExit as exc:
        return exc.code, ""


def test_dry_run_prints_the_command_and_message_and_merges_nothing(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_main("12", "--dry-run")
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(
        "gh pr merge 12 --squash --subject 'feat(cli): add the stamp subcommand (#12)' "
        "--body-file - --match-head-commit abc123\n\n"
        "feat(cli): add the stamp subcommand (#12)\n\nWhy\n"
    )
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


def test_merge_passes_subject_body_and_head(tools: Tools) -> None:
    assert run_main("12")[0] == 0
    view, merge = tools.calls()
    assert view["argv"] == ["pr", "view", "12", "--json", sm.VIEW_FIELDS]
    assert merge["argv"] == [
        "pr", "merge", "12", "--squash",
        "--subject", "feat(cli): add the stamp subcommand (#12)",
        "--body-file", "-", "--match-head-commit", "abc123",
    ]  # fmt: skip
    assert str(merge["stdin"]).startswith("Why\nRunning `tool stamp`")
    assert str(merge["stdin"]).endswith(
        "[1]: https://github.com/tbhb-dev/tools/issues/12\n"
    )


def test_merge_scans_the_final_message(tools: Tools, tmp_path: Path) -> None:
    assert run_main("12")[0] == 0
    merge = tools.calls()[1]
    subject = "feat(cli): add the stamp subcommand (#12)"
    (scanned,) = tools.scanned()
    assert scanned["cwd"] == str(tmp_path / "repo")
    assert scanned["stdin"] == f"{subject}\n\n{merge['stdin']}"
    assert scanned["argv"][scanned["argv"].index("--config") + 1] == str(  # type: ignore[index]
        tmp_path / ".config" / "gitleaks" / "private.toml"
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_finding_in_the_current_body_stops_the_merge(
    tools: Tools,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    finding = '[{"RuleID": "some-rule", "StartLine": 3}]'
    (tools.bin / "gitleaks.spec").write_text(json.dumps([1, finding]))
    argv = ["12", "--dry-run"] if dry_run else ["12"]
    code, _ = run_main(*argv)
    assert code == (
        "refusing the squash message for PR #12: gitleaks finds a secret: "
        "rule some-rule on line 3"
    )
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]
    assert "gh pr merge" not in capsys.readouterr().out


def test_a_missing_gitleaks_stops_the_merge(tools: Tools) -> None:
    (tools.bin / "gitleaks").unlink()
    code, _ = run_main("12")
    assert str(code).startswith("gitleaks not found on PATH")
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


def test_a_missing_private_config_stops_the_merge(tools: Tools, tmp_path: Path) -> None:
    (tmp_path / ".config" / "gitleaks" / "private.toml").unlink()
    code, _ = run_main("12")
    assert "private.toml cannot be read" in str(code)
    assert tools.scanned() == []
    assert [call["argv"][1] for call in tools.calls()] == ["view"]  # type: ignore[index]


def test_merge_reports_gh_output_and_exit_code(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    tools.view()
    spec = json.loads((tools.bin / "gh.spec").read_text())
    tools.gh(**{**spec, "merge_rc": 4, "merge_out": "o\n", "merge_err": "e\n"})
    assert run_main("3")[0] == 4
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("o\n", "e\n")


def test_a_body_without_sections_merges_with_the_subject_only(tools: Tools) -> None:
    tools.view(body="")
    assert run_main("3")[0] == 0
    assert tools.calls()[1]["stdin"] == ""


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"isDraft": True}, "draft"),
        ({"state": "MERGED"}, "merged"),
        ({"mergeable": "CONFLICTING"}, "mergeable=CONFLICTING"),
        ({"mergeStateStatus": "BLOCKED"}, "mergeStateStatus=BLOCKED"),
        ({"mergeStateStatus": "BEHIND"}, "BEHIND"),
        (
            {
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [
                    {"name": "lint", "conclusion": "FAILURE"},
                    {"context": "ci/x", "state": "ERROR"},
                    {"name": "ok", "conclusion": "SUCCESS"},
                ],
            },
            "failing checks: lint, ci/x",
        ),
    ],
)
def test_unmergeable_prs_are_refused_with_gh_s_reason(
    tools: Tools, fields: dict[str, object], reason: str
) -> None:
    tools.view(**fields)
    code, _ = run_main("3")
    assert isinstance(code, str)
    assert code.startswith("not merging PR #3: ")
    assert reason in code
    if "statusCheckRollup" in fields:
        assert code == (
            "not merging PR #3: gh reports mergeStateStatus=BLOCKED; "
            "failing checks: lint, ci/x"
        )
    assert len(tools.calls()) == 1


def test_a_clean_pr_with_odd_rollup_entries_is_fine() -> None:
    assert sm.failing_checks("nope") == []
    assert sm.failing_checks(["x", {"conclusion": "SUCCESS"}]) == []


def test_gh_view_failure_is_reported(tools: Tools) -> None:
    tools.gh(view_rc=1, view_err="no\nsuch pr\n")
    assert run_main("3")[0] == "gh pr view failed: no such pr"


def test_gh_view_garbage_is_reported(tools: Tools) -> None:
    tools.gh(view_out="<html>")
    code = run_main("3")[0]
    assert isinstance(code, str)
    assert code.startswith("gh pr view returned something that is not JSON")


def test_missing_fields_default_to_empty_and_refuse(tools: Tools) -> None:
    tools.gh(view_out="{}")
    code = run_main("3")[0]
    assert isinstance(code, str)
    assert "the PR is " in code


def test_null_body_is_empty(tools: Tools) -> None:
    tools.view(body=None)
    assert run_main("3")[0] == 0


# ------------------------------------------------------------------ Renovate

# The shape of a real Renovate body (tbhb-dev/tools#6), trimmed, with its CRLF
# line endings.
RENOVATE_BODY = """\
This PR contains the following updates:

| Package | Update | Change |
|---|---|---|
| [macos](https://redirect.github.com/actions/runner-images) | major | `15` → `26` |

---

### Release Notes

<details>
<summary>actions/runner-images (macos)</summary>

| Announcements |
|---|
| macOS 26 is now generally available |

<table><tr><td>Kotlin</td><td>2.3.21</td></tr></table>

</details>

---

### Configuration

🚦 **Automerge**: Disabled by config.

 - [ ] If you want to rebase/retry this PR, check this box
""".replace("\n", "\r\n")
RENOVATE_AUTHOR = {"is_bot": True, "login": "app/tbhb-dev-renovate"}


def test_renovate_body_keeps_only_the_update_table() -> None:
    assert sm.renovate_body(RENOVATE_BODY) == "- macos 15 → 26 (major)\n"


def test_renovate_body_lists_every_row_and_tolerates_missing_columns() -> None:
    body = (
        "Intro.\n\n| Package | Change |\n|---|---|\n"
        "| [a](https://x) | [`1` → `2`](https://y) |\n| b | `3` → `4` |\n"
        "\nafter\n| Package | Update | Change |\n|---|---|---|\n| c | major | 5 |\n"
    )
    assert sm.renovate_body(body) == "- a 1 → 2\n- b 3 → 4\n"
    assert sm.renovate_body("| Package |\n|---|\n| only |\n") == "- only\n"


def test_renovate_body_lists_a_repeated_update_once() -> None:
    body = (
        "| Package | Type | Update | Change |\n|---|---|---|---|\n"
        "| [ubuntu](https://x) |  | major | `24.04` → `26.04` |\n"
        "| [ubuntu](https://x) | github-runner | major | `24.04` → `26.04` |\n"
        "| [macos](https://x) |  | major | `15` → `26` |\n"
    )
    assert sm.renovate_body(body) == (
        "- ubuntu 24.04 → 26.04 (major)\n- macos 15 → 26 (major)\n"
    )


@pytest.mark.parametrize(
    "body", ["", "No table here.\n", "| Name | Change |\n|---|---|\n| a | 1 |\n"]
)
def test_renovate_body_without_the_update_table_is_empty(body: str) -> None:
    assert sm.renovate_body(body) == ""


def test_renovate_body_with_an_empty_table_is_empty() -> None:
    assert sm.renovate_body("| Package | Change |\n|---|---|\n\ntext\n") == ""


def test_a_renovate_pr_merges_with_the_update_list(tools: Tools) -> None:
    tools.view(
        title="chore(deps): update dependency macos to v26",
        body=RENOVATE_BODY,
        author=RENOVATE_AUTHOR,
    )
    assert run_main("6")[0] == 0
    assert tools.calls()[1]["stdin"] == "- macos 15 → 26 (major)\n"


@pytest.mark.parametrize(
    "author",
    [
        {"is_bot": False, "login": "app/tbhb-dev-renovate"},
        {"is_bot": True, "login": "app/dependabot"},
        {"login": "app/tbhb-dev-renovate"},
        "app/tbhb-dev-renovate",
        None,
    ],
)
def test_only_the_renovate_app_gets_the_short_body(
    tools: Tools, author: object
) -> None:
    tools.view(body="## Why\n\nBecause.\n", author=author)
    assert run_main("6")[0] == 0
    assert tools.calls()[1]["stdin"] == "Why\nBecause.\n"


def test_gh_is_asked_for_the_author() -> None:
    assert "author" in sm.VIEW_FIELDS.split(",")


# ------------------------------------------------------------------ the agent


def test_there_is_no_signoff_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        sm.main(["12", "--dry-run", "--signoff"], prog="merge-preview")
    assert info.value.code == 2
    assert capsys.readouterr().err.startswith("usage: merge-preview ")


@pytest.mark.parametrize(
    "env",
    [
        HARNESS_ENV,
        {"CODEX_SESSION_ID": "s"},
        {"ANTIGRAVITY_CONVERSATION_ID": "c"},
    ],
)
def test_an_agent_environment_changes_nothing(
    tools: Tools, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main("12")[0] == 0
    plain = tools.calls()
    (tools.bin / "gh.log").unlink()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert run_main("12")[0] == 0
    assert tools.calls() == plain
    assert [call["argv"][1] for call in plain] == ["view", "merge"]  # type: ignore[index]


def test_a_dry_run_is_the_same_in_an_agent_environment(
    tools: Tools, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main("12", "--dry-run")[0] == 0
    plain = capsys.readouterr().out
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "raw")
    assert run_main("12", "--dry-run")[0] == 0
    assert capsys.readouterr().out == plain
    assert [call["argv"][1] for call in tools.calls()] == ["view", "view"]  # type: ignore[index]


def test_the_script_never_adds_human_trailers(tools: Tools) -> None:
    assert run_main("12")[0] == 0
    stdin = str(tools.calls()[1]["stdin"])
    assert not re.search(r"Signed-off-by|Co-authored-by", stdin, re.IGNORECASE)


@pytest.mark.usefixtures("tools")
def test_runs_as_tbhbdev_alpha_merge(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": pythonpath(SRC)}
    env.pop("MUTANT_UNDER_TEST", None)
    done = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "alpha", "merge", "3", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path / "repo",
        env=env,
    )
    assert done.returncode == 0
    assert "gh pr merge 3" in done.stdout


def test_the_fallback_identity_block_is_kept_and_passes_the_check() -> None:
    block = (
        "Generated-by: LLM effort=medium;harness=claude-code:2.1.316;"
        "model=anthropic:m;session=sha-256-64:71c761039ac2c7f9;surface=cli"
    )
    message = message_of(f"## Why\n\nw\n\n{block}\n")
    assert message.endswith(f"\n\n{block}\n")
    assert read_back(message) == f"{block}\n"
    assert agent_identity.check_message(message) == []


def test_one_line_paragraphs_still_wrap_at_72() -> None:
    sentence = " ".join(["word"] * 60)
    out = message_of(f"## Why\n\n{sentence}\n\n- {sentence}\n").splitlines()
    assert len(out) > 8
    assert all(len(line) <= sm.WIDTH for line in out)
    assert out[3].startswith("word word")
    assert any(line.startswith("  word") for line in out)


# ------------------------------------------------------------------ opened by

OPENED = "Opened-by: LLM harness=claude-code:2.1.0;session=sha-256-64:aaaaaaaaaaaaaaaa"


@pytest.mark.parametrize("tail", ["\n", "\n\n\n", "\n  \n"])
def test_the_opened_by_line_is_dropped_and_the_trailers_still_checked(
    tail: str,
) -> None:
    message = message_of(f"## Why\n\nw\n\n{OPENED}\n{GEN}\n{ASSIST}{tail}")
    assert message == f"fix: x (#7)\n\nWhy\nw\n\n{GEN}\n{ASSIST}\n"
    assert read_back(message) == f"{GEN}\n{ASSIST}\n"


def test_an_opened_by_line_anywhere_in_the_trailer_paragraph_is_dropped() -> None:
    message = message_of(f"## Why\n\nw\n\n{GEN}\n{OPENED}\n{ASSIST}\n")
    assert message == f"fix: x (#7)\n\nWhy\nw\n\n{GEN}\n{ASSIST}\n"


def test_an_opened_by_line_alone_leaves_no_trailer_block() -> None:
    assert message_of(f"## Why\n\nw\n\n{OPENED}\n") == "fix: x (#7)\n\nWhy\nw\n"
    assert message_of(f"{OPENED}\n{GEN}\n") == f"fix: x (#7)\n\n{GEN}\n"


def test_an_invalid_trailer_after_opened_by_is_still_refused() -> None:
    with pytest.raises(SystemExit, match="invalid trailer"):
        message_of(f"## Why\n\nw\n\n{OPENED}\nGenerated-by: nonsense\n")


@pytest.mark.parametrize(
    "body",
    [
        f"## Why\n\n{OPENED}\n\nw\n\n{GEN}\n",
        f"## Why\n\nw\n{OPENED}\n{GEN}\n",
        f"## Why\n\n```\n{OPENED}\n```\n\n{GEN}\n",
        f"## Why\n\n```\n{OPENED}\n```\n",
    ],
)
def test_an_opened_by_line_outside_the_trailer_paragraph_is_refused(
    body: str,
) -> None:
    with pytest.raises(SystemExit, match="the script writes Opened-by"):
        message_of(body)


def test_a_body_with_opened_by_merges_without_it(tools: Tools) -> None:
    tools.view(body=f"## Why\n\nw\n\n{OPENED}\n{GEN}\n")
    assert sm.main(["7"]) == 0
    merge = tools.calls()[-1]
    assert cast("list[str]", merge["argv"])[:2] == ["pr", "merge"]
    assert merge["stdin"] == f"Why\nw\n\n{GEN}\n"


@pytest.mark.usefixtures("tools")
def test_every_subject_problem_is_listed() -> None:
    title = "BAD " + "a" * 72
    with pytest.raises(SystemExit) as info:
        sm.build_message(title, 5, "")
    assert str(info.value) == (
        f"the squash subject '{title} (#5)' fails: title is 81 characters; "
        "the limit is 72; title is not a Conventional Commit header (cog verify): "
        "not conventional"
    )


def test_an_unclosed_fence_names_its_line() -> None:
    with pytest.raises(SystemExit) as info:
        message_of("## Why\n\n```\ncode\n")
    assert str(info.value) == (
        "the PR body is malformed: line 3: code fence is never closed"
    )


def repo_reads(tools: Tools) -> list[dict[str, object]]:
    log = tools.bin / "gh.repo.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def scan_config(tools: Tools) -> str:
    """The config the one gitleaks call ran under."""
    (scanned,) = tools.scanned()
    argv = cast("list[str]", scanned["argv"])
    return argv[argv.index("--config") + 1]


def full_config() -> str:
    """The private config, under the test's HOME."""
    return str(Path.home() / ".config" / "gitleaks" / "private.toml")


@pytest.mark.parametrize("argv", [("12",), ("12", "--dry-run")])
@pytest.mark.parametrize(
    ("out", "rc", "reduced"),
    [
        ("PRIVATE\n", 0, True),
        ("PUBLIC\n", 0, False),
        ("INTERNAL\n", 0, False),
        ("", 0, False),
        ("weird\n", 0, False),
        ("PRIVATE\n", 1, False),
    ],
)
def test_the_visibility_is_read_once_and_picks_the_config(
    tools: Tools, argv: tuple[str, ...], out: str, rc: int, reduced: bool
) -> None:
    spec = tools.bin / "gh.spec"
    answer = {"repo_out": out, "repo_rc": rc, "repo_err": "boom" if rc else ""}
    spec.write_text(json.dumps(json.loads(spec.read_text()) | answer))
    assert run_main(*argv)[0] == 0
    (read,) = repo_reads(tools)
    assert read["argv"] == [
        "repo", "view", TARGET, "--json", "visibility", "--jq", ".visibility"
    ]  # fmt: skip
    assert (scan_config(tools) != full_config()) is reduced


def test_a_private_working_directory_merging_into_a_public_repo_gets_the_full_config(
    tools: Tools,
) -> None:
    other = "tbhb-dev/elsewhere"
    tools.view(url=f"https://github.com/{other}/pull/12")
    assert run_main("12")[0] == 0
    (read,) = repo_reads(tools)
    assert read["argv"][2] == other  # type: ignore[index]
    assert scan_config(tools) == full_config()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"url": ""},
        {"url": 7},
        {"url": "not a url"},
        {"url": "https://github.com/o/n/pull/x"},
        {"url": "http://github.com/o/n/pull/12"},
        {"url": "https://gitlab.com/o/n/pull/12"},
        {"url": "https://github.com/o/n/issues/12"},
    ],
)
def test_an_unresolved_target_uses_the_full_config_and_reads_nothing(
    tools: Tools, fields: dict[str, object]
) -> None:
    data = json.loads(json.loads((tools.bin / "gh.spec").read_text())["view_out"])
    data.pop("url")
    tools.gh(view_out=json.dumps(data | fields), repo_out="PRIVATE\n")
    assert run_main("12")[0] == 0
    assert repo_reads(tools) == []
    assert scan_config(tools) == full_config()


def test_the_view_reads_the_url() -> None:
    assert "url" in sm.VIEW_FIELDS.split(",")


AGENT_EMAIL = agent_identity.AGENT_IDENT.partition("<")[2].removesuffix(">")
AGENT_COMMITS = [
    {"authors": [{"email": "human@example.com"}]},
    {"authors": [{"email": "other@example.com"}, {"email": AGENT_EMAIL}]},
]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        (AGENT_COMMITS, "## Why\n\nw\n", "missing final trailer paragraph"),
        (
            AGENT_COMMITS,
            f"## Why\n\nw\n\n{ASSIST}\n{GEN}\n",
            "Generated-by must come before Assisted-by",
        ),
        (
            [],
            f"## Why\n\nw\n\n{ASSIST}\n{GEN}\n",
            "Generated-by must come before Assisted-by",
        ),
    ],
)
def test_invalid_pr_trailers_refuse_before_preview_or_merge(
    tools: Tools,
    capsys: pytest.CaptureFixture[str],
    case: tuple[list[dict[str, list[dict[str, str]]]], str, str],
    dry_run: bool,
) -> None:
    commits, body, reason = case
    tools.view(commits=commits, body=body)
    args = ["12", "--dry-run"] if dry_run else ["12"]
    code, _ = run_main(*args)
    assert isinstance(code, str)
    assert reason in code
    if reason == "missing final trailer paragraph":
        assert code == (
            "the PR body cannot be merged as is: "
            "agent PR is missing final trailer paragraph; "
            "add a valid trailer paragraph with tbhbdev alpha pr edit"
        )
    assert "tbhbdev alpha pr edit" in code
    assert capsys.readouterr().out == ""
    (view,) = tools.calls()
    assert cast("list[str]", view["argv"])[:2] == ["pr", "view"]
    assert tools.scanned() == []


@pytest.mark.parametrize("block", [GEN, ASSIST, f"{GEN}\n{ASSIST}"])
def test_an_agent_pr_merges_with_its_body_trailers(tools: Tools, block: str) -> None:
    tools.view(commits=AGENT_COMMITS, body=f"## Why\n\nw\n\n{block}\n")
    assert sm.main(["12"]) == 0
    view, merge = tools.calls()
    assert cast("list[str]", view["argv"])[-1] == sm.VIEW_FIELDS
    assert "commits" in sm.VIEW_FIELDS.split(",")
    assert merge["stdin"] == f"Why\nw\n\n{block}\n"
    assert read_back(f"fix: x\n\n{merge['stdin']}") == f"{block}\n"


@pytest.mark.parametrize(
    "commits",
    [[], [{"authors": []}], [{"authors": [{"email": "human@example.com"}]}]],
)
def test_a_non_agent_pr_needs_no_trailer_block(
    tools: Tools, commits: list[dict[str, list[dict[str, str]]]]
) -> None:
    tools.view(commits=commits, body="## Why\n\nw\n")
    assert sm.main(["12"]) == 0
    assert tools.calls()[1]["stdin"] == "Why\nw\n"


def test_renovate_keeps_its_path_even_with_an_agent_commit(tools: Tools) -> None:
    tools.view(
        commits=AGENT_COMMITS,
        author=RENOVATE_AUTHOR,
        body=RENOVATE_BODY,
    )
    assert sm.main(["12"]) == 0
    assert tools.calls()[1]["stdin"] == "- macos 15 → 26 (major)\n"


@pytest.mark.parametrize("agent", [False, True])
def test_fetch_records_the_pr_from_one_read(tools: Tools, agent: bool) -> None:
    commits = (
        AGENT_COMMITS if agent else [{"authors": [{"email": "human@example.com"}]}]
    )
    tools.view(
        title="fix: retain trailers",
        body="description",
        headRefOid="deadbeef",
        state="CLOSED",
        isDraft=True,
        mergeable="CONFLICTING",
        mergeStateStatus="BLOCKED",
        statusCheckRollup=[{"name": "lint", "conclusion": "FAILURE"}],
        author=RENOVATE_AUTHOR,
        commits=commits,
    )
    assert sm.fetch(42) == sm.PullRequest(
        title="fix: retain trailers",
        body="description",
        head="deadbeef",
        state="CLOSED",
        draft=True,
        mergeable="CONFLICTING",
        merge_state="BLOCKED",
        failing=["lint"],
        renovate=True,
        url=f"https://github.com/{TARGET}/pull/12",
        agent=agent,
    )
    (view,) = tools.calls()
    assert view == {
        "argv": ["pr", "view", "42", "--json", sm.VIEW_FIELDS],
        "stdin": "",
    }


@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("trailers", [[GEN], [ASSIST], [GEN, ASSIST]])
def test_valid_final_trailers_are_accepted(agent: bool, trailers: list[str]) -> None:
    assert sm.check_trailers(trailers, [], agent=agent) is None


def test_an_agent_pr_requires_a_final_trailer_paragraph() -> None:
    with pytest.raises(SystemExit) as info:
        sm.check_trailers([], [], agent=True)
    assert str(info.value) == (
        "the PR body cannot be merged as is: "
        "agent PR is missing final trailer paragraph; "
        "add a valid trailer paragraph with tbhbdev alpha pr edit"
    )


@pytest.mark.parametrize("agent", [False, True])
def test_valid_final_trailers_do_not_excuse_a_stray_trailer(agent: bool) -> None:
    lines = [pr.Line(1, GEN, GEN, code=False)]
    with pytest.raises(SystemExit) as info:
        sm.check_trailers([GEN], lines, agent=agent)
    assert str(info.value) == (
        "the PR body cannot be merged as is: "
        "the script writes Generated-by and Assisted-by; remove this line; "
        "add a valid trailer paragraph with tbhbdev alpha pr edit"
    )


def test_trailer_problems_report_the_full_block_and_repair_command() -> None:
    sm.check_trailers([], [])
    with pytest.raises(SystemExit) as info:
        sm.check_trailers(["Assisted-by: nope", GEN], [])
    assert str(info.value) == (
        "the PR body cannot be merged as is: invalid trailer block: "
        "Assisted-by: nope: must start with literal LLM; invalid trailer block: "
        f"{GEN}: Generated-by must come before Assisted-by; "
        "add a valid trailer paragraph with tbhbdev alpha pr edit"
    )


def test_fetch_defaults_are_exact(tools: Tools) -> None:
    tools.gh(view_out="{}")
    assert sm.fetch(12) == sm.PullRequest(
        title="",
        body="",
        head="",
        state="",
        draft=False,
        mergeable="",
        merge_state="",
        failing=[],
        renovate=False,
        url=None,
        agent=False,
    )


def test_dry_run_preview_is_exact(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    tools.view(title="fix: x", body="## Why\n\nw\n")
    assert sm.main(["12", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "gh pr merge 12 --squash --subject 'fix: x (#12)' "
        "--body-file - --match-head-commit abc123\n\n"
        "fix: x (#12)\n\nWhy\nw\n"
    )


# ------------------------------------------------ fetch mutation regressions


def test_fetch_converts_text_fields_to_strings(tools: Tools) -> None:
    tools.view(
        title=12,
        body=34,
        headRefOid=56,
        state=78,
        mergeable=90,
        mergeStateStatus=123,
    )
    request = sm.fetch(12)
    assert (
        request.title,
        request.body,
        request.head,
        request.state,
        request.mergeable,
        request.merge_state,
    ) == ("12", "34", "56", "78", "90", "123")


@pytest.mark.parametrize("body", [None, False, 0, ""])
def test_fetch_normalizes_a_falsy_body(tools: Tools, body: object) -> None:
    tools.view(body=body)
    assert sm.fetch(12).body == ""


@pytest.mark.parametrize("draft", [None, False, 0, 1, "true", "false"])
def test_fetch_requires_literal_true_for_a_draft(tools: Tools, draft: object) -> None:
    tools.view(isDraft=draft)
    assert sm.fetch(12).draft is False


@pytest.mark.parametrize(
    "author",
    [
        {"is_bot": 1, "login": "app/tbhb-dev-renovate"},
        {"is_bot": "true", "login": "app/tbhb-dev-renovate"},
        {"is_bot": True},
        {"is_bot": True, "login": None},
        {"is_bot": True, "login": 12},
        {"is_bot": True, "login": "app/tbhb-dev-RENOVATE"},
    ],
)
def test_fetch_does_not_mistake_other_author_metadata_for_renovate(
    tools: Tools, author: object
) -> None:
    tools.view(author=author)
    assert sm.fetch(12).renovate is False


@pytest.mark.usefixtures("tools")
@pytest.mark.parametrize("returncode", [2, -15])
def test_fetch_reports_every_nonzero_exit_with_normalized_stderr(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    done = subprocess.CompletedProcess(
        [], returncode, stdout="{}", stderr=" \tno\n such\r\npr  \t"
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return done

    monkeypatch.setattr(pr, "run", fake_run)
    with pytest.raises(SystemExit) as info:
        sm.fetch(12)
    assert info.value.code == "gh pr view failed: no such pr"


def test_fetch_includes_the_json_decode_error(tools: Tools) -> None:
    tools.gh(view_out="{")
    with pytest.raises(SystemExit) as info:
        sm.fetch(12)
    assert info.value.code == (
        "gh pr view returned something that is not JSON: "
        "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
    )


# ---------------------------------------------------------- main regressions


def test_main_reads_process_arguments_and_keeps_the_program_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["merge-preview", "--help"])
    with pytest.raises(SystemExit) as info:
        sm.main(prog="custom-merge")
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: custom-merge [-h] [--dry-run] PR\n")
    assert "Squash-merge a PR with a clean message." in out


def test_main_reports_every_refusal_before_scanning_or_printing(
    tools: Tools, capsys: pytest.CaptureFixture[str]
) -> None:
    tools.view(
        state="CLOSED",
        isDraft=True,
        mergeable="CONFLICTING",
        mergeStateStatus="BLOCKED",
    )
    with pytest.raises(SystemExit) as info:
        sm.main(["37", "--dry-run"])
    assert str(info.value) == (
        "not merging PR #37: the PR is closed; the PR is a draft; "
        "gh reports mergeable=CONFLICTING; gh reports mergeStateStatus=BLOCKED"
    )
    assert capsys.readouterr().out == ""
    assert len(tools.calls()) == 1
    assert repo_reads(tools) == []
    assert tools.scanned() == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_main_passes_the_full_message_and_target_visibility_to_the_scan(
    tools: Tools, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    tools.view(title="fix: x", body="## Why\n\nw\n")
    spec = tools.bin / "gh.spec"
    answer = {"repo_out": "PRIVATE\n"}
    spec.write_text(json.dumps(json.loads(spec.read_text()) | answer))
    scans: list[tuple[str, str, str]] = []

    def scan(text: str, what: str, visibility: str) -> None:
        scans.append((text, what, visibility))

    monkeypatch.setattr(pr, "scan_secrets", scan)
    args = ["37", "--dry-run"] if dry_run else ["37"]
    assert sm.main(args) == 0
    assert scans == [
        ("fix: x (#37)\n\nWhy\nw\n", "the squash message for PR #37", "PRIVATE")
    ]
