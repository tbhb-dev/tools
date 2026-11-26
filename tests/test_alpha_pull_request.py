# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
# cspell:ignore Ftopic

"""Tests for tbhbdev alpha pr, the pull request draft tooling."""

import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import chdir
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import pytest
from fake_tool import PR_ORIGIN as ORIGIN
from fake_tool import (
    PRIVATE_CONFIG,
    RULES_BEYOND_ONE,
    link_git,
    pythonpath,
    write_fake,
    write_private_config,
)
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from tbhbdev.alpha import identity as agent_identity
from tbhbdev.alpha import opened, size_gate
from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha.secret_scan import REPO_RULES, without_name_rules

# mutmut runs the tests from a copy of the source tree, so everything resolves
# against this file: the real repository root, or the mutants/ copy of it.
REAL = Path(__file__).resolve().parents[1]
SRC = Path(pr.__file__).parents[2]
DATA = Path(__file__).parent / "testdata" / "alpha" / "pull_request"
FIXTURE = DATA / "passing-draft.md"
TEMPLATE = REAL / ".github" / "PULL_REQUEST_TEMPLATE.md"
with chdir(REAL):
    SECTIONS = pr.load_sections()

H = "sha-256-64:"
SESSION = H + "a" * 16
SUBAGENT = H + "b" * 16
FALLBACK = (
    "LLM effort=medium;harness=claude-code:2.1.316;"
    "session=sha-256-64:71c761039ac2c7f9;surface=cli"
)
GEN_LINE = "Generated-by: LLM harness=claude-code;model=anthropic:m1"
GEN = "LLM harness=claude-code;model=anthropic:m1"
OTHER = "LLM harness=codex;model=openai:gpt-6.1-sol"

BASE = "# fix: x\n\n## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
# Line numbers in BASE: title 1, Why 3, Behavior change 7, Verification 11.

FAKE = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.basename(__file__).removesuffix(".py")
spec = json.load(open(os.path.join(here, name + ".spec")))
called = " ".join(sys.argv[1:])
cases = spec.get("cases", {{}})
if called == "pr view --json number,state,url":
    missing = {{"rc": 1, "stdout": "", "stderr": "No Pull Requests Found"}}
    spec = cases.get(called, missing)
elif called.startswith("api --hostname github.com --paginate --slurp "):
    spec = cases.get(called, {{
        "rc": 0, "stdout": '[[{{"commit": {{"message": "plain"}}}}]]', "stderr": ""
    }})
else:
    spec = next((v for k, v in cases.items() if called.startswith(k)), spec)
draft = ".github/PULL_REQUEST_DRAFT.md"
record = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "stdin_tty": sys.stdin.isatty(),
    "config": (
        open(sys.argv[sys.argv.index("--config") + 1]).read()
        if "--config" in sys.argv
        else None
    ),
    "draft": open(draft).read() if os.path.exists(draft) else None,
    "links": {{n: os.readlink(n) for n in os.listdir(".") if os.path.islink(n)}},
}}
# The visibility read has its own log, so it never shifts the other gh calls.
repo = ".repo" if called.startswith("repo view") else ""
with open(os.path.join(here, name + repo + ".log"), "a") as log:
    log.write(json.dumps(record) + "\\n")
sys.stdout.write(spec["stdout"])
sys.stderr.write(spec["stderr"])
sys.exit(spec["rc"])
"""


TARGET = "tbhb-dev/public-target"
PR_COMMITS_CALL = ["api", "--hostname", "github.com", "--paginate", "--slurp"]
BRANCH_LOOKUP = (
    "api --hostname github.com repos/tbhb-dev/example/branches/agent%2Ftopic"
)


def pr_page(repo: str, number: int = 7) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def view_case(url: object, body: object = "") -> dict[str, object]:
    """The fake gh answer to ``gh pr view N --json body,url``; no ``url`` omits it."""
    data: dict[str, object] = {"body": body}
    if url is not None:
        data["url"] = url
    return {"rc": 0, "stdout": json.dumps(data), "stderr": ""}


def base_case(name: object, visibility: object, rc: int = 0) -> dict[str, object]:
    """The fake gh answer to ``gh repo view --json nameWithOwner,visibility``."""
    data = {"nameWithOwner": name, "visibility": visibility}
    return {
        "rc": rc,
        "stdout": json.dumps(data) if rc == 0 else "",
        "stderr": "boom" if rc else "",
    }


@dataclass
class World:
    """A temp repository, a bin directory of fake tools, and an isolated PATH."""

    root: Path
    bin: Path

    def fake(
        self,
        name: str,
        *,
        rc: int = 0,
        stdout: str = "",
        stderr: str = "",
        cases: dict[str, dict[str, object]] | None = None,
    ) -> None:
        """A fake tool; ``cases`` answers the calls whose words start with a key."""
        write_fake(self.bin / name, FAKE.format())
        spec = {"rc": rc, "stdout": stdout, "stderr": stderr, "cases": cases or {}}
        (self.bin / f"{name}.spec").write_text(json.dumps(spec))

    def calls(self, name: str) -> list[dict[str, object]]:
        log = self.bin / f"{name}.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(self, message: str) -> None:
        self.git("commit", "--allow-empty", "-q", "-m", message)


# The world fixture replaces the counter; a test can put this one back.
REAL_SIZE_COUNTS = size_gate.size_counts


@pytest.fixture(autouse=True)
def host_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the home directory and git identity synthetic for every draft check."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "gitconfig"
    config.write_text("[user]\nname = Example\nemail = example@example.test\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def world_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """Give each test its own draft files, fake tools, logs, and home."""
    monkeypatch.setattr(
        size_gate,
        "size_counts",
        Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None)),
    )
    monkeypatch.setattr(pr, "target_is_origin", Mock(return_value=True))
    root = tmp_path / "repo"
    tools_dir = tmp_path / "bin"
    tools_dir.mkdir()
    # One statement, not a loop: under Bazel's clean environment there is
    # nothing to remove, and a loop body that never runs is a missed branch.
    prefixes = ("CLAUDE", "CODEX", "ANTIGRAVITY", "GIT_", "ASCM_")
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(prefixes)]))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "T")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")
    (root / ".github").mkdir(parents=True)
    shutil.copytree(REAL / ".github" / "schemas", root / ".github" / "schemas")
    shutil.copy(REAL / pr.SECTIONS, root / pr.SECTIONS)
    for name in pr.CONFIGS:
        (root / name).write_text("")
    (root / ".gitleaks.toml").write_text("")
    write_private_config(tmp_path, PRIVATE_CONFIG + RULES_BEYOND_ONE)
    made = World(root, tools_dir)
    made.fake("cog")
    made.fake("bazelisk")
    made.fake("scc")
    made.fake("vale", stdout="{}")
    made.fake("cspell")
    made.fake("rumdl")
    made.fake("gitleaks", stdout="[]")
    made.fake(
        "gh",
        stdout="https://github.com/tbhb-dev/tools/pull/1\n",
        cases={
            "pr view": view_case(pr_page(TARGET)),
            f"repo view {TARGET}": {"rc": 0, "stdout": "PUBLIC\\n", "stderr": ""},
            "repo view --json": base_case(TARGET, "PUBLIC"),
        },
    )
    return made


@pytest.fixture
def world(
    pr_git_baseline: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> World:
    """Copy Git metadata for tests that exercise real refs or worktrees."""
    made = world_files(tmp_path, monkeypatch)
    git_dir = tmp_path / "git_dir"
    link_git(git_dir)
    monkeypatch.setenv("PATH", f"{made.bin}{os.pathsep}{git_dir}")
    shutil.copytree(pr_git_baseline, made.root / ".git")
    monkeypatch.chdir(made.root)
    real_git = pr.git

    def offline_git(*argv: str, cwd: Path | None = None) -> tuple[int, str]:
        # Preserve the seeded tracking refs without contacting the fake origin.
        if argv[:1] == ("fetch",):
            return 0, ""
        return real_git(*argv, cwd=cwd)

    monkeypatch.setattr(pr, "git", offline_git)
    return made


@pytest.fixture
def draft_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """Run draft checks with fake tools and fixed answers for Git lookups."""
    made = world_files(tmp_path, monkeypatch)
    git_dir = tmp_path / "git_dir"
    link_git(git_dir)
    monkeypatch.setenv("PATH", f"{made.bin}{os.pathsep}{git_dir}")
    monkeypatch.chdir(made.root)
    base = "refs/remotes/origin/main"
    fetch_options = ("--quiet", "--no-tags", "--no-write-fetch-head", "--refmap=")
    answers: dict[tuple[str, ...], tuple[int, str]] = {
        ("rev-parse", "--show-toplevel"): (0, str(made.root.resolve())),
        ("ls-remote", "--symref", "origin", "HEAD"): (128, ""),
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (0, base),
        ("fetch", *fetch_options, "origin", f"+refs/heads/main:{base}"): (0, ""),
        ("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"): (0, ""),
    }

    def answer_git(*argv: str, **_kwargs: Path | None) -> tuple[int, str]:
        return answers[argv]

    monkeypatch.setattr(pr, "git", Mock(side_effect=answer_git))
    return made


def no_lint(_rendered: str, _first: int) -> list[pr.Problem]:
    return []


def no_title_problems(_title: str) -> list[str]:
    return []


def structure(text: str) -> list[pr.Problem]:
    """Check a draft without the external tools."""
    with (
        patch.object(pr, "lint", no_lint),
        patch.object(pr, "title_problems", no_title_problems),
    ):
        return pr.check_text(text).problems


# ----------------------------------------------------------- source of truth


def test_sections_file_matches_template_guidance() -> None:
    template = TEMPLATE.read_text()
    headings = re.findall(r"^## (.+)$", template, re.MULTILINE)
    assert headings == [section.heading for section in SECTIONS]
    guidance = re.findall(
        r"^## .+\n\n<!-- (Required|Optional), at most (\d+) words",
        template,
        re.MULTILINE,
    )
    assert [(g[0] == "Required", int(g[1])) for g in guidance] == [
        (section.required, section.max_words) for section in SECTIONS
    ]


def test_template_has_no_provenance_section() -> None:
    assert "Provenance" not in TEMPLATE.read_text().split("## Why")[1]


def test_limits() -> None:
    assert {s.heading: s.max_words for s in SECTIONS} == {
        "Why": 60,
        "Behavior change": 120,
        "Decisions": 120,
        "Deviations from the task": 80,
        "Needs human judgment": 100,
        "Risk": 80,
        "Verification": 150,
        "Human steering": 40,
    }
    assert [s.heading for s in SECTIONS if s.required] == [
        "Why",
        "Behavior change",
        "Verification",
    ]


def test_schema_is_valid_and_closed() -> None:
    schema = pr.load_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert pr.schema_default(schema, "draft") is False
    properties = cast("dict[str, dict[str, str]]", schema["properties"])
    assert all(prop.get("description") for prop in properties.values())


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"base": "release/1.x", "draft": True},
        {"labels": ["bug", "good first issue"], "reviewers": ["tbhb", "org/team"]},
        {"assignees": ["@me", "tbhb"], "milestone": "v1 beta"},
    ],
)
def test_schema_accepts(meta: dict[str, object]) -> None:
    assert Draft202012Validator(pr.load_schema()).is_valid(meta)


@pytest.mark.parametrize(
    "meta",
    [
        {"title": "x"},
        {"base": ""},
        {"base": "-x"},
        {"draft": "yes"},
        {"labels": ["a,b"]},
        {"labels": [" a"]},
        {"labels": ["a", "a"]},
        {"labels": ["x" * 51]},
        {"reviewers": ["bad name"]},
        {"reviewers": ["-x"]},
        {"assignees": ["@you"]},
        {"milestone": " v1"},
        {"milestone": ""},
    ],
)
def test_schema_rejects(meta: dict[str, object]) -> None:
    assert not Draft202012Validator(pr.load_schema()).is_valid(meta)


# -------------------------------------------------------------- word counting


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("", 0),
        ("one two  three\nfour", 4),
        ("- a\n- b", 2),
        ("-- ... *", 0),
        ("`code words` plain", 1),
        ("``a ` b`` plain", 1),
        ("[link text](https://example.com/a b) after", 3),
        ("![alt text](img.png)", 2),
        ("[text][ref] more\n\n[ref]: https://example.com", 2),
        ("<https://example.com> see https://example.com/x now", 2),
        ("a <!-- hidden words --> b", 2),
        ("a <!-- never closed\nhidden", 1),
        ("co-op don't 3rd", 3),
    ],
)
def test_count_words(text: str, count: int) -> None:
    assert pr.count_words(text) == count


WORD = st.text(alphabet="abcdefghij", min_size=1, max_size=6)


@given(st.lists(WORD, max_size=30))
def test_count_words_counts_words(words: list[str]) -> None:
    assert pr.count_words(" ".join(words)) == len(words)


@given(st.lists(WORD, max_size=10), WORD, WORD)
def test_free_text_adds_nothing(words: list[str], inner: str, url: str) -> None:
    text = " ".join(words)
    free = [
        f"`{inner} {inner}`",
        f"<!-- {inner} -->",
        f"<https://example.com/{url}>",
        f"https://example.com/{url}",
    ]
    assert pr.count_words(text + " " + " ".join(free)) == len(words)
    assert pr.count_words(f"[{inner}](https://example.com/{url})") == 1


# ----------------------------------------------------------- lines and fences


def test_fence_flags() -> None:
    raw = ["a", "```py", "x", "```", "b", "~~~", "y", "```", "~~~~", "c"]
    flags, unclosed = pr.fence_flags(raw)
    assert flags == [False, True, True, True, False, True, True, True, True, False]
    assert unclosed is None


@pytest.mark.parametrize(
    ("raw", "unclosed"),
    [
        (["a", "```", "x"], 1),
        (["```", "x", "``` info", "y"], 0),
        (["````", "x", "```", "y"], 0),
        (["   ```", "x", "   ```"], None),
        (["    ```", "x"], None),
        (["~~~", "```", "~~~"], None),
    ],
)
def test_fence_unclosed(raw: list[str], unclosed: int | None) -> None:
    assert pr.fence_flags(raw)[1] == unclosed


def test_strip_comments_keeps_line_numbers() -> None:
    texts = ["a <!-- x", "y --> b", "c", "<!-- z -->"]
    clean, starts = pr.strip_comments(texts)
    assert clean == ["a ", " b", "c", ""]
    assert starts == [0, 3]
    assert pr.strip_comments([]) == ([], [])
    assert pr.strip_comments(["a <!-- open", "b"])[0] == ["a ", ""]


def test_scan_reports_comments_and_fences() -> None:
    lines, problems = pr.scan(["a", "<!-- c -->", "```", "<!-- no -->", "x"], 10)
    assert problems == [
        (11, "HTML comment left in the description"),
        (12, "code fence is never closed"),
    ]
    assert [(line.no, line.text, line.code) for line in lines] == [
        (10, "a", False),
        (11, "", False),
        (12, "", True),
        (13, "", True),
        (14, "", True),
    ]
    assert lines[1].raw == "<!-- c -->"


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("# T", (1, "T")),
        ("## Two words ", (2, "Two words")),
        ("###### six", (6, "six")),
        ("   ## indented", (2, "indented")),
        ("    ## code", None),
        ("#nospace", None),
        ("####### seven", None),
        ("plain", None),
        ("#", None),
    ],
)
def test_heading_of(text: str, found: tuple[int, str] | None) -> None:
    assert pr.heading_of(pr.Line(1, text, text, code=False)) == found


def test_heading_of_ignores_code() -> None:
    assert pr.heading_of(pr.Line(1, "## x", "", code=True)) is None
    assert pr.heading_of(pr.Line(1, "## x", "## x", code=True)) is None


@given(st.lists(st.sampled_from(["a", "", "x y", "<!-- c -->"]), max_size=12))
def test_scan_keeps_one_line_per_input(raw: list[str]) -> None:
    lines, _ = pr.scan(raw, 5)
    assert [line.no for line in lines] == list(range(5, 5 + len(raw)))
    assert [line.raw for line in lines] == raw


def sections_text(names: list[str], bodies: list[str]) -> str:
    return "".join(f"## {n}\n\n{b}\n\n" for n, b in zip(names, bodies, strict=True))


@given(
    st.lists(
        st.tuples(st.integers(0, len(SECTIONS) - 1), st.integers(1, 40)),
        unique_by=lambda pair: pair[0],
        max_size=len(SECTIONS),
    )
)
def test_block_problems_follow_the_scheme(chosen: list[tuple[int, int]]) -> None:
    chosen = sorted(chosen)
    names = [SECTIONS[index].heading for index, _ in chosen]
    bodies = [" ".join(["word"] * count) for _, count in chosen]
    lines, _ = pr.scan(sections_text(names, bodies).splitlines(), 1)
    _, blocks, _ = pr.split_blocks(lines)
    assert [block.heading for block in blocks] == names
    found = pr.block_problems(blocks, SECTIONS)
    over = [
        n
        for (index, count), n in zip(chosen, names, strict=True)
        if count > (SECTIONS[index].max_words or 0)
    ]
    assert [text.split("'")[1] for _, text in found] == over


@given(st.permutations(range(len(SECTIONS))))
def test_block_problems_flag_any_other_order(order: list[int]) -> None:
    names = [SECTIONS[index].heading for index in order]
    lines, _ = pr.scan(sections_text(names, ["x"] * len(names)).splitlines(), 1)
    _, blocks, _ = pr.split_blocks(lines)
    found = pr.block_problems(blocks, SECTIONS)
    assert (len(found) > 0) == (order != sorted(order))


# ---------------------------------------------------------- structure checks


def test_minimal_draft_passes() -> None:
    assert structure(BASE) == []


def test_fixture_structure_passes() -> None:
    assert structure(FIXTURE.read_text()) == []


def replace(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


WHY_61 = " ".join(["word"] * 61)
WHY_60 = " ".join(["word"] * 60)
CASES: list[tuple[str, str, list[pr.Problem]]] = [
    ("empty", "", [(1, "the first line after the frontmatter must be the H1 title")]),
    (
        "section first",
        "## Why\n\nw\n",
        [(1, "the first line after the frontmatter must be the H1 title")],
    ),
    (
        "code first",
        "```\nx\n```\n" + BASE,
        [(1, "the first line after the frontmatter must be the H1 title")],
    ),
    (
        "missing why",
        replace(BASE, "## Why\n\nw\n\n", ""),
        [(1, "missing required section 'Why'")],
    ),
    (
        "missing verification",
        replace(BASE, "\n## Verification\n\nv\n", ""),
        [(1, "missing required section 'Verification'")],
    ),
    (
        "unknown",
        BASE + "\n## Provenance\n\nx\n",
        [
            (
                15,
                "unknown section 'Provenance'; use: "
                + ", ".join(s.heading for s in SECTIONS),
            )
        ],
    ),
    (
        "out of order",
        BASE + "\n## Risk\n\nx\n",
        [(15, "section 'Risk' is duplicated or out of order")],
    ),
    (
        "duplicate",
        BASE + "\n## Verification\n\nagain\n",
        [(15, "section 'Verification' is duplicated or out of order")],
    ),
    (
        "empty section",
        BASE + "\n## Human steering\n",
        [(15, "section 'Human steering' is empty")],
    ),
    (
        "comment only section",
        BASE + "\n## Human steering\n\n<!-- later -->\n",
        [
            (17, "HTML comment left in the description"),
            (15, "section 'Human steering' is empty"),
        ],
    ),
    (
        "comment",
        replace(BASE, "\nw\n", "\nw <!-- hi -->\n"),
        [(5, "HTML comment left in the description")],
    ),
    (
        "second h1",
        BASE + "\n# Again\n",
        [(15, "only one H1 is allowed: the title")],
    ),
    (
        "stray text",
        replace(BASE, "# fix: x\n\n", "# fix: x\n\nstray words\n\n"),
        [(3, "text before the first section; put it under a heading")],
    ),
    (
        "unclosed fence",
        BASE + "\n```\ncode\n",
        [(15, "code fence is never closed")],
    ),
    (
        "limit",
        replace(BASE, "\nw\n", f"\n{WHY_61}\n"),
        [(3, "section 'Why' has 61 words (limit 60)")],
    ),
    (
        "trailer",
        BASE + "\nGenerated-by: LLM harness=claude-code\n",
        [(15, "the script writes Generated-by and Assisted-by; remove this line")],
    ),
    (
        "assisted trailer",
        BASE + "Assisted-by: LLM harness=codex\n",
        [(14, "the script writes Generated-by and Assisted-by; remove this line")],
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"), [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES]
)
def test_structure_problems(text: str, expected: list[pr.Problem]) -> None:
    assert sorted(structure(text)) == sorted(expected)


def test_limit_boundary() -> None:
    assert structure(replace(BASE, "\nw\n", f"\n{WHY_60}\n")) == []


def test_code_and_links_are_free() -> None:
    text = replace(
        BASE,
        "\nw\n",
        f"\n{WHY_60}\n\n```\n{WHY_61}\n```\n\n`{WHY_61}` https://example.com/a\n",
    )
    assert structure(text) == []


def test_code_only_section_is_not_empty() -> None:
    assert structure(replace(BASE, "\nv\n", "\n```\n$ go test\n```\n")) == []


def test_fenced_lines_are_not_headings_comments_or_trailers() -> None:
    fenced = "\n```\n## Not a section\n<!-- not a comment -->\nAssisted-by: x\n```\n"
    assert structure(BASE + fenced) == []


def test_h3_and_indented_headings() -> None:
    text = replace(BASE, "\nw\n", "\n### Detail\n\nw\n")
    assert structure(text) == []
    assert structure(replace(BASE, "## Why", "   ## Why")) == []
    assert structure(replace(BASE, "## Why", "    ## Why")) == [
        (1, "missing required section 'Why'"),
        (3, "text before the first section; put it under a heading"),
    ]


def test_title_may_follow_blank_lines() -> None:
    assert structure("\n\n" + BASE) == []
    assert structure("---\nbase: main\n---\n\n" + BASE) == []


@pytest.mark.parametrize("key", ["Signed-off-by", "Co-authored-by", "co-authored-by"])
@pytest.mark.parametrize("env", [{}, {"CLAUDE_CODE_SESSION_ID": "raw"}])
def test_human_trailers_are_rejected_for_everyone(
    key: str, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    text = BASE + f"\n{key}: A <a@example.com>\n"
    assert structure(text) == [(15, pr.HUMAN_TRAILER_MESSAGE)]


# ------------------------------------------------------------- frontmatter


def test_frontmatter_errors_point_at_the_key() -> None:
    text = "---\nbase: main\ndraft: maybe\nlabels: [a]\n---\n" + BASE
    found = structure(text)
    assert [line for line, _ in found] == [3]
    assert found[0][1].startswith("frontmatter $.draft: ")


def test_frontmatter_unknown_key_is_line_one() -> None:
    found = structure("---\ndraft: true\ntitle: x\n---\n" + BASE)
    assert [line for line, _ in found] == [1]
    assert "title" in found[0][1]


def test_frontmatter_errors_are_reported_per_key() -> None:
    found = structure("---\nlabels: [a, a]\ndraft: 1\n---\n" + BASE)
    assert sorted((line, text.split(":")[0]) for line, text in found) == [
        (2, "frontmatter $.labels"),
        (3, "frontmatter $.draft"),
    ]


def test_frontmatter_invalid_yaml() -> None:
    found = structure("---\nlabels: [a\n---\n" + BASE)
    assert [line for line, _ in found] == [1]
    assert found[0][1].startswith("frontmatter: invalid YAML")
    assert (
        structure("---\nbase: main\n")[0][1] == "frontmatter: unterminated frontmatter"
    )


def test_frontmatter_refuses_a_repeated_key() -> None:
    found = structure("---\nbase: main\nbase: dev\n---\n" + BASE)
    assert [line for line, _ in found] == [1]
    assert "duplicate key 'base'" in found[0][1]


def test_frontmatter_must_be_a_mapping() -> None:
    found = structure("---\n- a\n---\n" + BASE)
    assert [line for line, _ in found] == [1]


def test_empty_frontmatter_block_is_fine() -> None:
    assert structure("---\n---\n" + BASE) == []


def test_line_numbers_follow_the_frontmatter() -> None:
    text = "---\nbase: main\n---\n\n" + replace(BASE, "\nw\n", "\nw <!-- c -->\n")
    assert structure(text) == [(9, "HTML comment left in the description")]


# -------------------------------------------------------------- tool lookup


def worktree_tool(
    world: World,
    name: str,
    body: str = "raise SystemExit('unexpected shim invocation')",
) -> Path:
    shim = world.root / ".bazel_env" / "bin" / name
    shim.parent.mkdir(parents=True, exist_ok=True)
    write_fake(shim, body)
    return shim


def test_find_tool_prefers_path(draft_world: World) -> None:
    shim = worktree_tool(draft_world, "vale")
    draft_world.fake("mise", stdout="/mise/vale\n")
    assert pr.find_tool("vale") == str(draft_world.bin / "vale")
    (draft_world.bin / "vale").unlink()
    assert pr.find_tool("vale") == str(shim)
    assert draft_world.calls("mise") == []


def test_find_tool_prefers_the_worktree_shim_to_mise(
    draft_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = worktree_tool(draft_world, "cog")
    (draft_world.bin / "cog").unlink()
    draft_world.fake("mise", stdout="/mise/cog\n")
    monkeypatch.chdir(draft_world.root / ".github")
    assert pr.find_tool("cog") == str(shim)
    assert draft_world.calls("mise") == []


def test_find_tool_can_skip_the_bazel_shims(
    draft_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = worktree_tool(draft_world, "vale")
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{path}")
    assert pr.find_tool("vale") == str(shim)
    assert pr.find_tool("vale", unwrapped=True) == str(draft_world.bin / "vale")
    (draft_world.bin / "vale").unlink()
    draft_world.fake("mise", stdout="/mise/vale\n")
    monkeypatch.setenv("PATH", path)
    assert pr.find_tool("vale") == str(shim)
    assert pr.find_tool("vale", unwrapped=True) == "/mise/vale"


def test_find_tool_falls_back_to_mise(draft_world: World, tmp_path: Path) -> None:
    shim = worktree_tool(draft_world, "absent_tool")
    shim.chmod(0o644)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "absent_tool").write_text("")
    draft_world.fake("mise", stdout=f"{elsewhere / 'absent_tool'}\n")
    assert pr.find_tool("absent_tool") == str(elsewhere / "absent_tool")
    assert draft_world.calls("mise")[0]["argv"] == ["which", "absent_tool"]
    shim.chmod(0o755)
    assert pr.find_tool("absent_tool") == str(shim)


def test_find_tool_missing(draft_world: World) -> None:
    with pytest.raises(
        SystemExit, match=r"absent_tool not found on PATH or through `mise which`"
    ):
        pr.find_tool("absent_tool")
    draft_world.fake("mise", rc=1, stderr="not installed")
    with pytest.raises(SystemExit, match="absent_tool not found"):
        pr.find_tool("absent_tool")
    draft_world.fake("mise", rc=0, stdout="\n")
    with pytest.raises(SystemExit, match="absent_tool not found"):
        pr.find_tool("absent_tool")


def test_find_tools_returns_each_path(world: World) -> None:
    shim = worktree_tool(world, "cog")
    (world.bin / "cog").unlink()
    worktree_tool(world, "gitleaks")
    (world.bin / "gitleaks").unlink()
    world.fake("mise", stdout="/mise/gitleaks\n")
    assert pr.find_tools(["cog", "vale", "gitleaks"], unwrapped={"gitleaks"}) == {
        "cog": str(shim),
        "vale": str(world.bin / "vale"),
        "gitleaks": "/mise/gitleaks",
    }


@pytest.mark.usefixtures("world")
def test_find_tools_reports_all_missing_names() -> None:
    with pytest.raises(SystemExit) as error:
        pr.find_tools(["first", "vale", "second"])
    assert str(error.value) == (
        "missing tools: first, second; "
        "run `bazelisk run //:bazel_env` or `mise install`"
    )


# ------------------------------------------------------------------- title


def test_title_goes_to_cog_on_stdin(draft_world: World) -> None:
    assert pr.title_problems("feat: x") == []
    call = draft_world.calls("cog")[0]
    assert call["argv"] == ["verify", "--file", "-"]
    assert call["stdin"] == "feat: x"
    assert call["cwd"] == str(draft_world.root.resolve())


MISSING = (
    "title is not a Conventional Commit header (cog verify): Error: Missing commit type"
)


def test_title_cog_failure_is_reported(draft_world: World) -> None:
    draft_world.fake("cog", rc=1, stderr="Error: Missing\n\n  commit type\n")
    assert pr.title_problems("Add thing") == [MISSING]


@pytest.mark.usefixtures("draft_world")
def test_title_length() -> None:
    assert pr.MAX_TITLE == 64
    assert pr.title_problems("feat: " + "x" * 58) == []
    assert pr.title_problems("feat: " + "x" * 59) == [
        "title is 65 characters; the limit is 64"
    ]


def test_title_problem_lands_on_the_title_line(draft_world: World) -> None:
    draft_world.fake("cog", rc=1, stderr="bad")
    found = pr.check_text("---\nbase: main\n---\n\n" + BASE).problems
    assert found == [(5, "title is not a Conventional Commit header (cog verify): bad")]


@pytest.mark.skipif(shutil.which("cog") is None, reason="cog is not installed")
@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("feat(cli)!: add x", True),
        ("fix: y", True),
        ("Add thing", False),
        ("wip: x", False),
    ],
)
def test_real_cog(
    title: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ok: bool
) -> None:
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "example@example.test"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    assert (pr.title_problems(title) == []) is ok


# -------------------------------------------------------------------- lint


def lint_checked(world: World, text: str) -> list[pr.Problem]:
    del world
    return pr.check_text(text).problems


def test_lint_runs_each_tool_in_a_mirrored_tree(draft_world: World) -> None:
    text = "---\nbase: main\n---\n\n" + BASE
    assert lint_checked(draft_world, text) == []
    rendered = text.split("---\n\n", 1)[1]
    for name, argv in (
        ("vale", ["--output=JSON", ".github/PULL_REQUEST_DRAFT.md"]),
        (
            "cspell",
            [
                "--config",
                ".cspell.jsonc",
                "--no-summary",
                "--no-progress",
                ".github/PULL_REQUEST_DRAFT.md",
            ],
        ),
        ("rumdl", ["check", ".github/PULL_REQUEST_DRAFT.md"]),
    ):
        (call,) = draft_world.calls(name)
        assert call["argv"] == argv
        assert call["draft"] == rendered
        assert call["stdin"] == ""
        assert call["links"] == {
            config: str(draft_world.root / config) for config in pr.CONFIGS
        }
        assert call["cwd"] != str(draft_world.root)


def test_lint_maps_lines_back_to_the_draft(draft_world: World) -> None:
    draft_world.fake(
        "vale",
        rc=1,
        stdout=json.dumps(
            {
                "f": [
                    {"Check": "Google.We", "Message": "no we", "Line": 3},
                    {
                        "Check": "project.HeadingTitleCase",
                        "Message": "Title",
                        "Line": 1,
                    },
                    {
                        "Check": "project.HeadingTitleCase",
                        "Message": "Title",
                        "Line": 7,
                    },
                ]
            }
        ),
    )
    draft_world.fake(
        "cspell",
        rc=1,
        stdout=".github/PULL_REQUEST_DRAFT.md:5:2 - Unknown word (zzz)\n",
    )
    draft_world.fake(
        "rumdl",
        rc=1,
        stdout=(
            ".github/PULL_REQUEST_DRAFT.md:9:1: [MD022] spacing [*]\n\n"
            "Issues: Found 1 issue\n"
        ),
    )
    found = lint_checked(draft_world, "---\nbase: main\n---\n\n" + BASE)
    assert found == [
        (7, "vale Google.We: no we"),
        (9, "cspell: Unknown word (zzz)"),
        (11, "vale project.HeadingTitleCase: Title"),
        (13, "rumdl: [MD022] spacing [*]"),
    ]


def test_cspell_and_rumdl_lines_ignore_other_output() -> None:
    parse = pr.parse_lines(pr.CSPELL_LINE, "cspell")
    assert parse("noise\nCSpell: Files checked: 1\n") == []
    assert parse("a.md:3:4 - Unknown word (x)") == [(3, "cspell: Unknown word (x)")]


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr"),
    [
        (2, "", "Configuration Error: bad"),
        (1, "not json", ""),
        (3, "", "killed"),
    ],
)
def test_lint_tool_failure_is_a_problem(
    draft_world: World, rc: int, stdout: str, stderr: str
) -> None:
    draft_world.fake("vale", rc=rc, stdout=stdout, stderr=stderr)
    found = lint_checked(draft_world, BASE)
    message = stderr or stdout
    assert found == [(1, f"vale failed (exit {rc}): {message}")]


def test_lint_failure_message_prefers_stderr(draft_world: World) -> None:
    draft_world.fake("cspell", rc=2, stdout="out", stderr="err  text")
    assert lint_checked(draft_world, BASE) == [(1, "cspell failed (exit 2): err text")]


def test_lint_failure_with_findings_reports_the_failure_only(
    draft_world: World,
) -> None:
    draft_world.fake("rumdl", rc=2, stdout="a.md:3:1: [MD001] x\n", stderr="boom")
    assert lint_checked(draft_world, BASE) == [(1, "rumdl failed (exit 2): boom")]


def test_vale_parse_handles_bad_json() -> None:
    assert pr.parse_vale("{") == []
    assert pr.parse_vale("{}") == []


def test_lint_needs_every_tool_and_config(draft_world: World) -> None:
    (draft_world.bin / "rumdl").unlink()
    with pytest.raises(SystemExit, match="rumdl not found"):
        lint_checked(draft_world, BASE)
    draft_world.fake("rumdl")
    (draft_world.root / ".rumdl.toml").unlink()
    with pytest.raises(SystemExit, match=r"missing lint config \.rumdl\.toml in "):
        lint_checked(draft_world, BASE)


VALE_INI = """StylesPath = .vale
Packages = https://example.com/Google.zip, \\
  https://example.com/write-good.zip

[*.md]
BasedOnStyles = Vale, Google,  write-good, project

[tmp/**]
BasedOnStyles =

[*.txt]
BasedOnStyles = Google, extra
"""


def styled(world: World, *present: str) -> None:
    """A .vale.ini naming four styles, with only ``present`` synced."""
    (world.root / ".vale.ini").write_text(VALE_INI)
    (world.root / ".vale").unlink()
    for name in present:
        (world.root / ".vale" / name).mkdir(parents=True)


SYNCED = '("Google", "write-good", "extra")'
SYNCS = """import os, sys
open(os.path.join(os.path.dirname(__file__), "bazelisk.log"), "a").write(
    " ".join(sys.argv[1:]) + "|" + os.getcwd() + "\\n"
)
if sys.argv[1:] == ["query", "//:vale_sync"]:
    print("//:vale_sync")
    sys.exit(0)
for name in SYNCED:
    os.makedirs(os.path.join(".vale", name), exist_ok=True)
"""


def test_vale_styles_names_each_style_once(draft_world: World) -> None:
    styled(draft_world)
    assert pr.vale_styles(draft_world.root) == [
        "Google",
        "extra",
        "project",
        "write-good",
    ]


def test_vale_styles_split_at_the_first_equals(draft_world: World) -> None:
    # Vale's ini parser splits a key from its value at the first "=".
    (draft_world.root / ".vale.ini").write_text("BasedOnStyles = odd=name\n")
    assert pr.vale_styles(draft_world.root) == ["odd=name"]


def test_synced_styles_need_no_sync(draft_world: World) -> None:
    styled(draft_world, "Google", "extra", "project", "write-good")
    assert lint_checked(draft_world, BASE) == []
    assert not (draft_world.bin / "bazelisk.log").exists()
    assert len(draft_world.calls("vale")) == 1


def test_missing_styles_sync_once_then_lint(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    styled(world, "project")
    write_fake(world.bin / "bazelisk", SYNCS.replace("SYNCED", SYNCED))
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root / "sub")  # the sync runs at the top, not here
    assert lint_checked(world, BASE) == []
    log = (world.bin / "bazelisk.log").read_text().splitlines()
    assert log == [
        f"query //:vale_sync|{world.root.resolve()}",
        f"run //:vale_sync|{world.root.resolve()}",
    ]
    assert len(world.calls("vale")) == 1


@pytest.mark.parametrize(
    ("rc", "stdout"), [(0, ""), (0, "//:vale_sync_other\n"), (7, "//:vale_sync\n")]
)
def test_missing_sync_target_is_one_problem(
    draft_world: World, rc: int, stdout: str
) -> None:
    styled(draft_world, "project", "extra")
    draft_world.fake("bazelisk", rc=rc, stdout=stdout)
    assert lint_checked(draft_world, BASE) == [
        (
            1,
            (
                "vale: vale styles missing from .vale/: Google, write-good; "
                "missing Bazel target //:vale_sync"
            ),
        )
    ]
    (call,) = draft_world.calls("bazelisk")
    assert call["argv"] == ["query", "//:vale_sync"]
    assert call["cwd"] == str(draft_world.root.resolve())
    assert draft_world.calls("vale") == []
    assert len(draft_world.calls("cspell")) == 1


@pytest.mark.parametrize(
    ("stdout", "stderr", "why"),
    [("out", "no  such target", "no such target"), ("out  text", "", "out text")],
)
def test_failed_sync_is_one_problem(
    draft_world: World, stdout: str, stderr: str, why: str
) -> None:
    styled(draft_world, "project", "extra")
    draft_world.fake(
        "bazelisk",
        rc=3,
        stdout=stdout,
        stderr=stderr,
        cases={"query": {"rc": 0, "stdout": "//:vale_sync\n", "stderr": ""}},
    )
    found = lint_checked(draft_world, BASE)
    assert found == [
        (
            1,
            (
                "vale: vale styles missing from .vale/: Google, write-good; "
                f"`bazelisk run //:vale_sync` exited 3: {why}"
            ),
        )
    ]
    query, call = draft_world.calls("bazelisk")
    assert query["argv"] == ["query", "//:vale_sync"]
    assert call["argv"] == ["run", "//:vale_sync"]
    assert draft_world.calls("vale") == []
    assert len(draft_world.calls("cspell")) == 1


def test_sync_that_leaves_styles_missing_is_a_problem(draft_world: World) -> None:
    styled(draft_world, "project")
    # Exit 0, but it only syncs Google.
    write_fake(draft_world.bin / "bazelisk", SYNCS.replace("SYNCED", '("Google",)'))
    found = lint_checked(draft_world, BASE)
    assert found == [
        (
            1,
            (
                "vale: vale styles missing from .vale/: extra, write-good; "
                "`bazelisk run //:vale_sync` exited 0: "
            ),
        )
    ]


def test_missing_bazelisk_stops_the_check(draft_world: World) -> None:
    styled(draft_world, "project")
    (draft_world.bin / "bazelisk").unlink()
    with pytest.raises(SystemExit, match="bazelisk not found"):
        lint_checked(draft_world, BASE)


def test_lint_skipped_without_a_title(draft_world: World) -> None:
    assert lint_checked(draft_world, "## Why\n\nw\n")
    assert draft_world.calls("vale") == []
    assert draft_world.calls("cog") == []


def real_lint_ready() -> bool:
    names = ("vale", "cspell", "rumdl", "cog")
    configs = all((REAL / name).exists() for name in pr.CONFIGS)
    styles = (REAL / ".vale" / "Google").is_dir()
    return configs and styles and all(shutil.which(name) for name in names)


@pytest.mark.skipif(
    not real_lint_ready(), reason="linters or their configs are missing"
)
class TestRealLinters:
    @pytest.fixture(autouse=True)
    def _real_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "lint-repo"
        root.mkdir()
        for name in [key for key in os.environ if key.startswith("GIT_")]:
            monkeypatch.delenv(name, raising=False)
        for variable in (
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            monkeypatch.setenv(variable, str(tmp_path / variable.lower()))
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Example"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "example@example.test"],
            cwd=root,
            check=True,
        )
        for name in pr.CONFIGS:
            source = REAL / name
            target = root / name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy(source, target)
        shutil.copytree(REAL / ".github", root / ".github")
        monkeypatch.chdir(root)

    def test_fixture_passes(self) -> None:
        # The title is a lowercase Conventional Commit header, which the
        # sentence-case rule would flag if it were not filtered.
        assert pr.check_text(FIXTURE.read_text()).problems == []

    def test_findings_map_to_draft_lines(self) -> None:
        # cspell:ignore recieve
        text = FIXTURE.read_text()
        text = replace(text, "Running `tool stamp`", "Running tool recieve")
        text = replace(text, "## Why\n\n", "## Why\n")
        found = pr.check_text(text).problems
        assert any(line == 15 and "recieve" in why for line, why in found)
        assert any(line == 14 and "rumdl" in why for line, why in found)


# ------------------------------------------------------------------ trailers


def value(**attrs: str) -> str:
    return agent_identity.format_value(attrs)


def test_identity_key_ignores_effort_surface_and_version() -> None:
    one = value(effort="low", harness="claude-code:1.0", model="a:m", surface="cli")
    two = value(effort="high", harness="claude-code:2.0", model="a:m")
    assert pr.identity_key(one) == pr.identity_key(two)
    assert pr.identity_key(one) == ("claude-code", "a:m", "", "")
    full = value(harness="codex", model="a:m", session=SESSION, subagent=SUBAGENT)
    assert pr.identity_key(full) == ("codex", "a:m", SESSION, SUBAGENT)
    assert pr.identity_key(value(harness="codex")) == ("codex", "", "", "")


def test_merge_dedupes_and_orders() -> None:
    a = value(harness="claude-code", model="a:m1")
    b = value(harness="codex", model="a:m2")
    found = [("Assisted-by", a), ("Generated-by", b), ("Assisted-by", a)]
    assert pr.merge_trailers(found, None) == [("Generated-by", b), ("Assisted-by", a)]


def test_generated_by_wins_whichever_comes_first() -> None:
    low = value(effort="low", harness="claude-code", model="a:m")
    high = value(effort="high", harness="claude-code", model="a:m")
    assert pr.merge_trailers([("Assisted-by", low), ("Generated-by", high)], None) == [
        ("Generated-by", high)
    ]
    assert pr.merge_trailers([("Generated-by", high), ("Assisted-by", low)], None) == [
        ("Generated-by", high)
    ]
    assert pr.merge_trailers([("Generated-by", high), ("Generated-by", low)], None) == [
        ("Generated-by", high)
    ]
    assert pr.merge_trailers([("Assisted-by", high), ("Assisted-by", low)], None) == [
        ("Assisted-by", high)
    ]


def test_posting_agent_is_added_as_assisted_once() -> None:
    a = value(harness="claude-code", model="a:m")
    me = value(harness="codex", model="o:m")
    assert pr.merge_trailers([("Generated-by", a)], me) == [
        ("Generated-by", a),
        ("Assisted-by", me),
    ]
    assert pr.merge_trailers([], me) == [("Assisted-by", me)]
    assert pr.merge_trailers([("Generated-by", me)], me) == [("Generated-by", me)]
    assert pr.merge_trailers([("Assisted-by", me)], me) == [("Assisted-by", me)]
    assert pr.merge_trailers([], None) == []


def identity(harness: str, model: str, session: str) -> str:
    extra = {"session": session} if session else {}
    return value(harness=harness, model=model, **extra)


IDENTITIES = st.builds(
    identity,
    st.sampled_from(["claude-code", "codex"]),
    st.sampled_from(["a:m1", "a:m2"]),
    st.sampled_from(["", SESSION, SUBAGENT]),
)
TRAILERS = st.lists(
    st.tuples(st.sampled_from(["Generated-by", "Assisted-by"]), IDENTITIES)
)


@given(TRAILERS, st.none() | IDENTITIES)
def test_merge_properties(found: list[tuple[str, str]], posting: str | None) -> None:
    merged = pr.merge_trailers(found, posting)
    keys = [pr.identity_key(v) for _, v in merged]
    assert len(keys) == len(set(keys))
    wanted = {pr.identity_key(v) for _, v in found}
    if posting is not None:
        wanted.add(pr.identity_key(posting))
    assert set(keys) == wanted
    roles = [k for k, _ in merged]
    assert roles == sorted(roles, key=lambda role: role != "Generated-by")
    generated = {pr.identity_key(v) for k, v in found if k == "Generated-by"}
    for (role, _), key in zip(merged, keys, strict=True):
        assert (role == "Generated-by") == (key in generated)
    assert not agent_identity.check_message("\n".join(f"{k}: {v}" for k, v in merged))


AGENT = {
    "CLAUDE_CODE_SESSION_ID": "raw-session",
    "CLAUDE_CODE_EXECPATH": "/x/2.1.0",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_EFFORT": "high",
}
POSTING = (
    "LLM effort=high;harness=claude-code:2.1.0;model=anthropic:claude-opus-5-5;"
    f"session={agent_identity.sha256_64('raw-session')};surface=cli"
)
ROOT = agent_identity.sha256_64("raw-session")
OPENER = (
    "Opened-by: LLM harness=claude-code:2.1.0;model=anthropic:claude-opus-5-5;"
    f"session={ROOT};surface=cli"
)


def ledgers(world: World) -> Path:
    """The directory of opened ledgers in the world's user store."""
    return world.root.parent / "state" / "tbhbdev" / "tbhb-alpha" / "opened"


def no_ancestry(*_args: object, **_kwargs: object) -> list[str]:
    return []


def seed_session(world: World, model: str | None = "claude-opus-5-5") -> None:
    """The posting session's record, with a transcript that has one reply."""
    transcript = world.root.parent / "session.jsonl"
    message = {"model": model} if model else {"content": "x"}
    transcript.write_text(json.dumps({"message": message}) + "\n")
    agent_identity.write_record(
        agent_identity.sha256_64("raw-session"),
        {"transcript": str(transcript)},
        world.root,
    )


@pytest.fixture
def agent(world: World, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(agent_identity, "ancestry", no_ancestry)
    seed_session(world)
    return AGENT


def test_posting_value(world: World, agent: dict[str, str]) -> None:
    del world
    assert pr.posting_value(agent) == POSTING
    assert pr.posting_value({}) is None


def test_branch_trailers_are_in_commit_order(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}\nAssisted-by: {OTHER}")
    world.commit("plain")
    world.commit(f"three\n\nSigned-off-by: T <t@example.com>\nGenerated-by: {OTHER}")
    assert pr.branch_trailers("main") == [
        ("Generated-by", GEN),
        ("Assisted-by", OTHER),
        ("Generated-by", OTHER),
    ]


def test_branch_trailers_stop_at_the_merge_base(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("switch", "-q", "main")
    world.commit(f"theirs\n\nGenerated-by: {OTHER}")
    world.git("switch", "-q", "topic")
    assert pr.branch_trailers("main") == [("Generated-by", GEN)]
    assert pr.branch_trailers("topic") == []


def test_branch_trailers_prefer_the_remote_base(world: World) -> None:
    world.commit(f"upstream\n\nGenerated-by: {OTHER}")
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("branch", "-f", "main", "HEAD~2")
    with patch.object(pr, "git", wraps=pr.git) as git:
        assert pr.branch_trailers("main") == [("Generated-by", GEN)]
    git.assert_any_call(
        "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main^{commit}"
    )


def test_branch_trailers_ignore_a_tag_named_like_the_remote_base(
    world: World,
) -> None:
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"mine\n\nGenerated-by: {GEN}")
    world.git("tag", "origin/main")
    assert pr.branch_trailers("main") == [("Generated-by", GEN)]


def test_branch_trailers_need_a_known_base(world: World) -> None:
    del world
    with pytest.raises(
        SystemExit, match=r"cannot list the commits in nope\.\.HEAD; is nope fetched"
    ):
        pr.branch_trailers("nope")


def checked_for() -> pr.Checked:
    return pr.check_text(BASE)


def test_description_without_trailers_is_the_body(world: World) -> None:
    del world
    got = pr.description(checked_for(), {}, [])
    assert got == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"


def test_description_appends_a_trailer_paragraph(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    got = pr.description(checked_for(), agent, pr.branch_trailers("main"))
    assert got.endswith(f"\nv\n\nGenerated-by: {GEN}\nAssisted-by: {POSTING}\n")
    parsed = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=got,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert parsed == f"Generated-by: {GEN}\nAssisted-by: {POSTING}\n"
    assert "Signed-off-by" not in got
    assert "raw-session" not in got


def test_human_posting_adds_only_branch_trailers(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    assert pr.description(checked_for(), {}, pr.branch_trailers("main")).endswith("v\n")
    world.commit(f"one\n\nAssisted-by: {GEN}")
    got = pr.description(checked_for(), {}, pr.branch_trailers("main"))
    assert got.endswith(f"v\n\nAssisted-by: {GEN}\n")
    assert got.count("Assisted-by") == 1


def test_posting_agent_already_on_the_branch_is_not_repeated(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {POSTING}")
    got = pr.description(checked_for(), agent, pr.branch_trailers("main"))
    assert got.endswith(f"v\n\nGenerated-by: {POSTING}\n")


def test_a_resolved_posting_value_is_one_line_with_its_commit_trailer(
    world: World, agent: dict[str, str]
) -> None:
    """The session's posting value has model=, so it matches its own commit."""
    assert "model=anthropic:claude-opus-5-5" in POSTING
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {POSTING}")
    got = pr.description(checked_for(), agent, pr.branch_trailers("main"))
    session = f"session={agent_identity.sha256_64('raw-session')}"
    lines = [line for line in got.splitlines() if session in line]
    assert lines == [f"Generated-by: {POSTING}"]
    assert "Assisted-by" not in got
    assert pr.posting_value(agent) == POSTING


def test_the_posting_session_dedupes_against_its_commit_trailer(
    world: World, agent: dict[str, str]
) -> None:
    """Regression: a posting value without model= added a second line."""
    write_draft(world)
    world.git("switch", "-q", "-c", "topic")
    # The trailer the session's commit got, from the same commit-time source.
    committed = agent_identity.committer_identity(agent, [])
    assert committed is not None
    trailer = agent_identity.format_value(committed.ident.attrs())
    assert "model=anthropic:claude-opus-5-5" in trailer
    world.commit(f"one\n\nGenerated-by: {trailer}")
    assert pr.posting_value(agent) == trailer
    merged = pr.merge_trailers(pr.branch_trailers("main"), pr.posting_value(agent))
    assert merged == [("Generated-by", trailer)]
    assert run_main("create", env=agent) == 0
    _, call = world.calls("gh")
    session = f"session={agent_identity.sha256_64('raw-session')}"
    lines = [line for line in str(call["stdin"]).splitlines() if session in line]
    assert lines == [OPENER, f"Generated-by: {trailer}"]


@pytest.mark.parametrize("argv", [["create"], ["edit", str(pr.DRAFT), "--pr", "7"]])
def test_an_unresolved_posting_model_fails_without_posting(
    world: World, agent: dict[str, str], argv: list[str]
) -> None:
    write_draft(world)
    seed_session(world, None)
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    posting = POSTING.replace("model=anthropic:claude-opus-5-5;", "")
    with pytest.raises(SystemExit) as info:
        run_main(*argv, env=agent)
    assert info.value.code == f"invalid LLM trailer: {posting}: model required"
    reads = (
        [
            ["pr", "view", "7", "--json", "body,url"],
            [*PR_COMMITS_CALL, f"repos/{TARGET}/pulls/7/commits"],
        ]
        if argv[0] == "edit"
        else []
    )
    assert [call["argv"] for call in world.calls("gh")] == reads


def test_description_uses_the_frontmatter_base(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    world.git("branch", "dev", "main")
    world.git("switch", "-q", "dev")
    world.commit(f"dev\n\nGenerated-by: {OTHER}")
    world.git("update-ref", "refs/remotes/origin/dev", "HEAD")
    world.git("switch", "-q", "-c", "topic2")
    world.commit(f"two\n\nAssisted-by: {GEN}")
    write_draft(world, "---\nbase: dev\n---\n" + BASE)
    assert run_main("create") == 0
    _, call = world.calls("gh")
    got = str(call["stdin"])
    assert got.endswith(f"v\n\nAssisted-by: {GEN}\n")
    assert OTHER not in got


@pytest.mark.parametrize(
    "bad", ["LLM model=a:m", "claude-code:opus", "LLM harness=nope"]
)
def test_invalid_branch_trailers_stop_the_description(world: World, bad: str) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {bad}")
    with pytest.raises(SystemExit, match=r"invalid LLM trailer: .*"):
        pr.description(checked_for(), {}, pr.branch_trailers("main"))


def test_invalid_trailer_message_names_the_value(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit("one\n\nGenerated-by: LLM model=a:m")
    with pytest.raises(SystemExit) as info:
        pr.description(checked_for(), {}, pr.branch_trailers("main"))
    assert str(info.value) == "invalid LLM trailer: LLM model=a:m: harness required"


# -------------------------------------------------------------- gh and the CLI


def run_main(*argv: str, env: dict[str, str] | None = None) -> int:
    return pr.main(list(argv), env or {})


def write_draft(world: World, text: str = BASE) -> Path:
    path = world.root / ".github" / "PULL_REQUEST_DRAFT.md"
    path.write_text(text)
    os.chdir(world.root)  # the default draft path is relative to the cwd
    return path


def test_check_prints_ok(
    draft_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    write_draft(draft_world)
    assert run_main("check") == 0
    assert capsys.readouterr().out == (
        "0 code lines, limit 400\n0 raw lines, limit 1500\nOK: the draft conforms\n"
    )


def test_check_prints_problems_with_paths(
    draft_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_draft(draft_world, replace(BASE, "\nw\n", "\nw <!-- c -->\n"))
    assert run_main("check", str(path)) == 1
    assert capsys.readouterr().out == (
        f"{path}:5: HTML comment left in the description\n"
        "0 code lines, limit 400\n0 raw lines, limit 1500\n"
    )


def test_check_defaults_to_the_draft_path(
    draft_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del draft_world
    assert run_main("check") == 1
    out = capsys.readouterr().out
    assert out == (
        ".github/PULL_REQUEST_DRAFT.md:1: "
        "cannot read the draft: No such file or directory\n"
        "0 code lines, limit 400\n0 raw lines, limit 1500\n"
    )


def test_create_runs_gh_with_the_frontmatter(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = (
        "---\nbase: dev\ndraft: true\nlabels: [bug, good first issue]\n"
        "reviewers: [tbhb, org/team]\nassignees: ['@me']\nmilestone: v1\n---\n"
    )
    world.git("update-ref", "refs/remotes/origin/dev", "HEAD")
    write_draft(world, meta + BASE)
    assert run_main("create") == 0
    _, call = world.calls("gh")
    assert call["argv"] == [
        "pr", "create", "--title", "fix: x", "--body-file", "-",
        "--base", "dev", "--draft",
        "--label", "bug", "--label", "good first issue",
        "--reviewer", "tbhb", "--reviewer", "org/team",
        "--assignee", "@me", "--milestone", "v1",
    ]  # fmt: skip
    assert (
        call["stdin"]
        == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    assert call["stdin_tty"] is False
    assert capsys.readouterr().out == "https://github.com/tbhb-dev/tools/pull/1\n"


def test_create_defaults(world: World) -> None:
    write_draft(world)
    assert run_main("create") == 0
    _, call = world.calls("gh")
    assert call["argv"] == [
        "pr",
        "create",
        "--title",
        "fix: x",
        "--body-file",
        "-",
        "--base",
        "main",
    ]


@pytest.mark.parametrize("ending", ["", "\n", "\n\n"])
def test_create_passes_gh_errors_through(
    world: World, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    message = "aborted: you must first push the current branch to a remote" + ending
    world.fake("gh", rc=1, stderr=message)
    write_draft(world)
    assert run_main("create") == 1
    captured = capsys.readouterr()
    assert captured.err == message.rstrip("\n") + "\n"
    assert captured.out == ""
    assert len(world.calls("gh")) == 2
    assert world.git("status", "--porcelain", "--ignored").count("push") == 0


def remote_default(world: World, tmp_path: Path, branch: str) -> None:
    remote = tmp_path / "remote.git"
    world.git("init", "-q", "--bare", "-b", branch, str(remote))
    world.git("push", "-q", str(remote), f"HEAD:refs/heads/{branch}")
    world.git("remote", "set-url", "origin", str(remote))


def test_a_draft_without_a_base_takes_the_default_branch(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted = Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None))
    monkeypatch.setattr(size_gate, "size_counts", counted)
    monkeypatch.setattr(pr, "branch_trailers", Mock(return_value=[]))
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    remote_default(world, tmp_path, "trunk")
    write_draft(world, BASE)
    run_main("check")
    counted.assert_called_once_with(
        "refs/remotes/origin/trunk",
        "HEAD",
        scc=str(world.bin / "scc"),
        git=str(world.root.parent / "git_dir" / "git"),
    )


def test_the_default_branch_ignores_a_tag_of_the_same_name(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted = Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None))
    monkeypatch.setattr(size_gate, "size_counts", counted)
    monkeypatch.setattr(pr, "branch_trailers", Mock(return_value=[]))
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    world.git("tag", "origin/trunk")
    remote_default(world, tmp_path, "trunk")
    assert pr.default_base() == "trunk"


def test_an_older_reader_still_loads_the_schema_default() -> None:
    """Installs that predate the default-branch change read `base`'s default."""
    assert pr.schema_default(pr.load_schema(), "base") == "main"


def test_the_remote_default_beats_a_stale_origin_head(
    world: World, tmp_path: Path
) -> None:
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    remote_default(world, tmp_path, "main")
    assert pr.default_base() == "main"


def test_an_unreachable_remote_falls_back_to_origin_head(
    world: World, tmp_path: Path
) -> None:
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    world.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    world.git("remote", "set-url", "origin", str(tmp_path / "missing.git"))
    assert pr.default_base() == "trunk"


def test_gh_repo_refuses_a_guessed_default_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_REPO", "other/repo")
    with pytest.raises(SystemExit, match="default branch of GH_REPO"):
        pr.default_base()


def unset_origin_head(world: World, url: str) -> None:
    world.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    world.git("remote", "set-url", "origin", url)


def test_an_unset_origin_head_reads_the_remotes_own_default(
    world: World, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    world.git("init", "-q", "--bare", "-b", "dev", str(remote))
    world.git("push", "-q", str(remote), "HEAD:refs/heads/dev")
    unset_origin_head(world, str(remote))
    assert pr.default_base() == "dev"


def test_create_reads_the_default_branch_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(["trunk", "dev", "other"])
    read = Mock(side_effect=lambda: next(answers))
    monkeypatch.setattr(pr, "default_base", read)
    counted = Mock(return_value=size_gate.SizeCounts(0, 400, 0, 1500, None))
    monkeypatch.setattr(size_gate, "size_counts", counted)
    trailers = Mock(return_value=[])
    monkeypatch.setattr(pr, "branch_trailers", trailers)
    world.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
    write_draft(world)
    assert run_main("create") == 0
    assert read.call_count == 1
    assert counted.call_args.args[0] == "refs/remotes/origin/trunk"
    trailers.assert_called_once_with("trunk")
    _, call = world.calls("gh")
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[-2:] == ["--base", "trunk"]


@pytest.mark.parametrize("kind", ["unreachable", "empty"])
def test_an_unreadable_remote_default_refuses_without_a_guess(
    world: World, tmp_path: Path, kind: str
) -> None:
    remote = tmp_path / "remote.git"
    if kind == "empty":
        world.git("init", "-q", "--bare", "-b", "dev", str(remote))
    unset_origin_head(world, str(remote))
    with pytest.raises(SystemExit, match="cannot find the default branch"):
        pr.default_base()


def test_create_stops_on_problems(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    write_draft(world, "## Why\n\nw\n")
    assert run_main("create") == 1
    assert world.calls("gh") == []
    assert ":1: the first line" in capsys.readouterr().out


def test_dry_run_prints_argv_and_body(
    world: World, capsys: pytest.CaptureFixture[str], agent: dict[str, str]
) -> None:
    write_draft(world, "---\nlabels: [bug]\n---\n" + BASE)
    assert run_main("create", "--dry-run", env=agent) == 0
    out = capsys.readouterr().out
    assert out == (
        "gh pr create --title 'fix: x' --body-file - --base main --label bug\n\n"
        "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n\n"
        f"{OPENER}\nAssisted-by: {POSTING}\n"
    )
    assert not ledgers(world).exists()


@pytest.mark.parametrize(
    ("verb", "extra"), [("check", ()), ("create", ("gitleaks", "gh"))]
)
def test_pr_reports_all_missing_tools_before_reading_the_draft(
    world: World, verb: str, extra: tuple[str, ...]
) -> None:
    missing = ("cog", "cspell", *extra)
    for name in missing:
        (world.bin / name).unlink()
    worktree_tool(world, "gitleaks")
    with pytest.raises(SystemExit) as error:
        run_main(verb)
    assert str(error.value) == (
        f"missing tools: {', '.join(missing)}; "
        "run `bazelisk run //:bazel_env` or `mise install`"
    )


@pytest.mark.parametrize("verb", ["check", "create"])
def test_pr_runs_without_scc_and_warns_about_the_size_count(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    # The world fixture stubs the counter; this test needs the real one.
    monkeypatch.setattr(size_gate, "size_counts", REAL_SIZE_COUNTS)
    (world.bin / "scc").unlink()
    write_draft(world)
    args = ("create", "--dry-run") if verb == "create" else ("check",)
    assert run_main(*args) == 0
    captured = capsys.readouterr()
    assert "missing tools" not in captured.err
    assert "size count unavailable: " in captured.err


def test_create_reuses_the_tools_resolved_at_startup(world: World) -> None:
    gh = world.bin / "gh"
    installed_gh = gh.with_name("installed-gh")
    gh.rename(installed_gh)
    git = world.root.parent / "git_dir" / "git"
    installed_git = git.with_name("installed-git")
    git.rename(installed_git)
    world.fake(
        "mise",
        cases={
            "which gh": {"rc": 0, "stdout": f"{installed_gh}\n", "stderr": ""},
            "which git": {"rc": 0, "stdout": f"{installed_git}\n", "stderr": ""},
        },
    )
    write_draft(world)
    assert run_main("create", "--dry-run") == 0
    assert [call["argv"] for call in world.calls("mise")] == [
        ["which", "git"],
        ["which", "gh"],
    ]
    installed_gh.rename(gh)
    assert pr.find_tool("gh") == str(gh)


def test_create_needs_gh(world: World) -> None:
    (world.bin / "gh").unlink()
    write_draft(world)
    with pytest.raises(SystemExit) as error:
        run_main("create")
    assert str(error.value) == (
        "missing tools: gh; run `bazelisk run //:bazel_env` or `mise install`"
    )


def test_edit(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    write_draft(
        world, "---\nlabels: [bug, docs]\ndraft: true\nbase: main\n---\n" + BASE
    )
    assert run_main("edit", str(pr.DRAFT), "--pr", "7") == 0
    _, _, call = world.calls("gh")
    assert call["argv"] == [
        "pr", "edit", "7", "--title", "fix: x", "--body-file", "-",
        "--add-label", "bug", "--add-label", "docs",
    ]  # fmt: skip
    assert (
        call["stdin"]
        == "## Why\n\nw\n\n## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    assert capsys.readouterr().out == "https://github.com/tbhb-dev/tools/pull/1\n"


def test_edit_without_labels_and_with_agent(
    world: World, agent: dict[str, str]
) -> None:
    write_draft(world)
    assert run_main("edit", str(pr.DRAFT), "--pr", "7", env=agent) == 0
    _, _, call = world.calls("gh")
    assert call["argv"] == ["pr", "edit", "7", "--title", "fix: x", "--body-file", "-"]
    assert str(call["stdin"]).endswith(f"v\n\nAssisted-by: {POSTING}\n")


def fake_pr_commits(
    world: World, number: int, stdout: str, *, rc: int = 0, stderr: str = ""
) -> list[str]:
    argv = [*PR_COMMITS_CALL, f"repos/{TARGET}/pulls/{number}/commits"]
    answer: dict[str, object] = {"rc": rc, "stdout": stdout, "stderr": stderr}
    view = view_case(pr_page(TARGET, number))
    world.fake("gh", cases={"pr view": view, " ".join(argv): answer})
    return argv


def test_edit_pr_40_uses_github_commit_trailers(
    world: World, agent: dict[str, str]
) -> None:
    local = (
        "LLM effort=medium;harness=codex:0.171.0;model=openai:gpt-6.2-sol;"
        "session=sha-256-64:ae40cbadd8ab72ec;surface=cli"
    )
    attrs = (
        "effort=medium;harness=claude-code;model=anthropic:claude-sonnet-5-5;"
        "session=sha-256-64:7ff1a5b545107a55;surface=cli"
    )
    merged = attrs.replace("7ff1a5b545107a55", "1234567890abcdef")
    world.git("switch", "-q", "-c", "other-branch")
    world.commit(f"other PR\n\nGenerated-by: {local}")
    pages = [
        [{"commit": {"message": f"one\n\nGenerated-by: LLM {attrs}"}}],
        [
            {"commit": {"message": f"two\n\nGenerated-by: LLM {attrs}"}},
            {
                "parents": [{"sha": "a" * 40}, {"sha": "b" * 40}],
                "commit": {"message": f"merge\n\n---\n\nGenerated-by: LLM\n {merged}"},
            },
            {"commit": {"message": f"body\n\nGenerated-by: {local}\n\nMore prose."}},
        ],
    ]
    argv = fake_pr_commits(world, 40, json.dumps(pages))
    write_draft(world)
    assert run_main("edit", str(pr.DRAFT), "--pr", "40", env=agent) == 0
    _, commits, edit = world.calls("gh")
    assert commits["argv"] == argv
    assert str(edit["stdin"]).split("\n\n")[-1] == (
        f"Generated-by: LLM {attrs}\nGenerated-by: LLM {merged}\n"
        f"Assisted-by: {POSTING}\n"
    )
    (scan,) = world.calls("gitleaks")
    assert scan["stdin"] == f"fix: x\n\n{edit['stdin']}"


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr", "why"),
    [
        (1, "[[{}]]", "no\naccess", "no access"),
        (0, "", "", "GitHub returned no commit list"),
        (0, "[[]]", "", "GitHub returned no commits"),
        (0, "[{}]", "", "GitHub returned invalid commit data"),
        (0, "[[1]]", "", "GitHub returned invalid commit data"),
        (0, '[[{"commit":{"message":1}}]]', "", "GitHub returned invalid commit data"),
    ],
)
def test_edit_refuses_unavailable_commits(
    world: World, rc: int, stdout: str, stderr: str, why: str
) -> None:
    argv = fake_pr_commits(world, 7, stdout, rc=rc, stderr=stderr)
    draft = write_draft(world)
    message = f"cannot list the commits of PR 7: {why}"
    with pytest.raises(SystemExit, match=f"^{re.escape(message)}$"):
        run_main("edit", str(pr.DRAFT), "--pr", "7")
    assert draft.read_text() == BASE
    assert [call["argv"] for call in world.calls("gh")] == [
        ["pr", "view", "7", "--json", "body,url"],
        argv,
    ]
    assert world.calls("gitleaks") == []


def test_edit_refuses_unreadable_trailers(world: World) -> None:
    world.fake("git", rc=1)
    write_draft(world)
    with pytest.raises(SystemExit, match=r"^cannot parse the commit trailers of PR 7$"):
        run_main("edit", str(pr.DRAFT), "--pr", "7")
    assert len(world.calls("gh")) == 2


def test_create_scans_the_final_title_and_body(
    world: World, agent: dict[str, str]
) -> None:
    write_draft(world)
    assert run_main("create", env=agent) == 0
    (call,) = world.calls("gitleaks")
    assert cast("list[str]", call["argv"])[:3] == ["stdin", "--redact", "--no-banner"]
    assert call["cwd"] == str(world.root)
    _, gh = world.calls("gh")
    assert call["stdin"] == f"fix: x\n\n{gh['stdin']}"
    assert str(call["stdin"]).endswith(f"Assisted-by: {POSTING}\n")


def test_edit_scans_the_final_title_and_body(world: World) -> None:
    write_draft(world)
    assert run_main("edit", str(pr.DRAFT), "--pr", "7") == 0
    (call,) = world.calls("gitleaks")
    _, _, gh = world.calls("gh")
    assert call["stdin"] == f"fix: x\n\n{gh['stdin']}"


FINDING = '[{"RuleID": "some-rule", "StartLine": 9}]'


@pytest.mark.parametrize(
    "argv", [("create",), ("create", "--dry-run"), ("edit", str(pr.DRAFT), "--pr", "7")]
)
def test_a_finding_stops_before_gh(
    world: World, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    world.fake("gitleaks", rc=1, stdout=FINDING)
    write_draft(world)
    with pytest.raises(SystemExit) as info:
        run_main(*argv)
    assert str(info.value) == (
        "refusing the PR title and body: gitleaks finds a secret: "
        "rule some-rule on line 9"
    )
    assert [call["argv"][:2] for call in world.calls("gh")] in (  # type: ignore[index]
        [],
        [["pr", "view"], ["api", "--hostname"]],
    )
    assert "gh pr" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [("create",), ("edit", str(pr.DRAFT), "--pr", "7")])
def test_a_missing_gitleaks_stops_before_gh(
    world: World, argv: tuple[str, ...]
) -> None:
    (world.bin / "gitleaks").unlink()
    write_draft(world)
    with pytest.raises(
        SystemExit, match=r"missing tools: gitleaks|gitleaks not found on PATH"
    ):
        run_main(*argv)
    assert [call["argv"][:2] for call in world.calls("gh")] in (  # type: ignore[index]
        [],
        [["pr", "view"], ["api", "--hostname"]],
    )


def test_a_missing_private_config_stops_before_gh(world: World) -> None:
    (Path.home() / ".config" / "gitleaks" / "private.toml").unlink()
    write_draft(world)
    with pytest.raises(SystemExit, match=r"private\.toml cannot be read"):
        run_main("create")
    assert world.calls("gitleaks") == []
    assert world.calls("gh") == []


def test_check_does_not_scan_or_read_the_visibility(draft_world: World) -> None:
    (draft_world.bin / "gh").unlink()
    (draft_world.bin / "gitleaks").unlink()
    write_draft(draft_world)
    assert run_main("check") == 0
    assert draft_world.calls("gitleaks") == []
    assert draft_world.calls("gh.repo") == []


def edit_cases(
    url: object, stdout: str = "PUBLIC\n", rc: int = 0
) -> dict[str, dict[str, object]]:
    """The PR's page is ``url``; its repository reads ``stdout``.

    The working directory's repository, which a read with no repository
    argument would name, is PRIVATE, so a command that reads it gets that.
    """
    return {
        "pr view": view_case(url),
        f"repo view {TARGET}": {
            "rc": rc,
            "stdout": stdout,
            "stderr": "boom" if rc else "",
        },
        "repo view --json": {"rc": 0, "stdout": "PRIVATE\n", "stderr": ""},
    }


VISIBILITY_READ = ["--json", "visibility", "--jq", ".visibility"]


@pytest.mark.parametrize(
    ("stdout", "rc", "want"),
    [("PRIVATE\n", 0, "PRIVATE"), ("", 0, ""), ("PRIVATE\n", 1, ""), ("", 1, "")],
)
def test_the_visibility_read_names_the_target_and_is_empty_when_it_fails(
    world: World, stdout: str, rc: int, want: str
) -> None:
    world.fake("gh", cases=edit_cases(None, stdout, rc))
    assert pr.repo_visibility(TARGET) == want
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", TARGET, *VISIBILITY_READ]


@pytest.mark.parametrize(
    "target",
    ["tbhb-dev/tools", "A1-b/.github", "o/a.b_c-d", "o/..a", "o/x", "9/9"],
)
def test_a_well_formed_target_is_read(world: World, target: str) -> None:
    answer: dict[str, object] = {"rc": 0, "stdout": "PRIVATE\n", "stderr": ""}
    world.fake("gh", cases={"repo view": answer})
    assert pr.repo_visibility(target) == "PRIVATE"
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", target, *VISIBILITY_READ]


@pytest.mark.parametrize(
    "target",
    [
        "",
        "tools",
        "o/",
        "/x",
        "o/n/extra",
        "-o/x",
        "o/.",
        "o/..",
        "o/x y",
        "o/x\n",
        "\no/x",
        "o/é",
        "é/x",
        "o x/y",
        "o/x;y",
    ],
)
def test_a_malformed_target_is_empty_without_a_gh_call(
    world: World, target: str
) -> None:
    answer: dict[str, object] = {"rc": 0, "stdout": "PRIVATE\n", "stderr": ""}
    world.fake("gh", cases={"repo view": answer})
    assert pr.repo_visibility(target) == ""
    assert world.calls("gh.repo") == []


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/tbhb-dev/tools/pull/12",
        "https://github.com/o/n/pull/0",
        "https://github.com/A1-b/.github/pull/123456",
    ],
)
def test_a_pr_url_names_its_repository(url: str) -> None:
    assert (
        pr.target_of_pr(url)
        == url.removeprefix("https://github.com/").split("/pull/")[0]
    )


@pytest.mark.parametrize(
    "url",
    [
        None,
        7,
        ["https://github.com/o/n/pull/1"],
        "",
        "not a url",
        "http://github.com/o/n/pull/1",
        "https://gitlab.com/o/n/pull/1",
        "https://githubXcom/o/n/pull/1",
        "https://github.com.evil.example/o/n/pull/1",
        "https://www.github.com/o/n/pull/1",
        "https://user@github.com/o/n/pull/1",
        "https://github.com:443/o/n/pull/1",
        "xhttps://github.com/o/n/pull/1",
        "https://github.com/o/n/pull/",
        "https://github.com/o/n/pull/x",
        "https://github.com/o/n/pull/1x",
        "https://github.com/o/n/pull/\u0661",
        "https://github.com/o/n/issues/1",
        "https://github.com/o/n/pulls/1",
        "https://github.com/o/n/pull/1/files",
        "https://github.com/o/n/pull/1?x=1",
        "https://github.com/o/n/pull/1#c",
        "https://github.com/o/n/pull/1\n",
        " https://github.com/o/n/pull/1",
        "https://github.com/o/../pull/1",
        "https://github.com/o/./pull/1",
        "https://github.com/o//pull/1",
        "https://github.com//n/pull/1",
        "https://github.com/pull/1",
        "https://github.com/-o/n/pull/1",
        "https://github.com/o/n/x/pull/1",
        "https://github.com/https://github.com/o/n/pull/1",
    ],
)
def test_a_url_that_is_not_a_github_pr_page_names_nothing(url: object) -> None:
    assert pr.target_of_pr(url) == ""


@pytest.mark.parametrize(
    ("answer", "want"),
    [
        (base_case(TARGET, "PRIVATE"), "PRIVATE"),
        (base_case(TARGET, "PUBLIC"), "PUBLIC"),
        (base_case(TARGET, ""), ""),
        (base_case(TARGET, None), ""),
        (base_case(TARGET, "PRIVATE", rc=1), ""),
        (base_case("", "PRIVATE"), ""),
        (base_case(None, "PRIVATE"), ""),
        (base_case(7, "PRIVATE"), ""),
        (base_case("a/b/c", "PRIVATE"), ""),
        (base_case("not a name", "PRIVATE"), ""),
        (base_case("o/..", "PRIVATE"), ""),
        ({"rc": 0, "stdout": "[]", "stderr": ""}, ""),
        ({"rc": 0, "stdout": "PRIVATE\n", "stderr": ""}, ""),
        ({"rc": 0, "stdout": '{"visibility": "PRIVATE"}', "stderr": ""}, ""),
        ({"rc": 0, "stdout": '{"nameWithOwner": "o/n"}', "stderr": ""}, ""),
    ],
)
def test_the_base_visibility_counts_only_with_a_parsed_name(
    world: World, answer: dict[str, object], want: str
) -> None:
    world.fake("gh", cases={"repo view": answer})
    assert pr.base_visibility() == want
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", "--json", "nameWithOwner,visibility"]


def without_path(config: str) -> str:
    """A config with its ``path`` lines dropped: the scan points them at a copy."""
    return re.sub(r"^\s*path\s*=.*$", "", config, flags=re.MULTILINE)


def scan_config(world: World) -> str:
    """The text of the config the one gitleaks call ran under, minus its paths."""
    (call,) = world.calls("gitleaks")
    return without_path(str(call["config"]))


def full_config() -> str:
    """The private config under the test's HOME, minus its paths.

    Every scan drops the repository rules, so this is the config a scan that
    keeps the host rules runs under.
    """
    path = Path.home() / ".config" / "gitleaks" / "private.toml"
    return without_path(str(without_name_rules(path.read_text(), REPO_RULES)))


VISIBILITIES = [
    ("PRIVATE\n", 0, True),
    ("PUBLIC\n", 0, False),
    ("INTERNAL\n", 0, False),
    ("", 0, False),
    ("weird\n", 0, False),
    ("PRIVATE\n", 1, False),
]


@pytest.mark.parametrize("argv", [("create",), ("create", "--dry-run")])
@pytest.mark.parametrize(("stdout", "rc", "reduced"), VISIBILITIES)
def test_create_reads_the_base_repository_once_and_picks_the_config(
    world: World, argv: tuple[str, ...], stdout: str, rc: int, reduced: bool
) -> None:
    cases = {"repo view": base_case(TARGET, stdout.strip(), rc)}
    world.fake("gh", stdout="u\n", cases=cases)
    write_draft(world)
    assert run_main(*argv) == 0
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", "--json", "nameWithOwner,visibility"]
    assert (scan_config(world) != full_config()) is reduced


@pytest.mark.parametrize("name", ["", None, "o/..", "a/b/c", 5, "not a name"])
def test_create_without_a_parsed_base_repository_uses_the_full_config(
    world: World, name: object
) -> None:
    world.fake("gh", stdout="u\n", cases={"repo view": base_case(name, "PRIVATE")})
    write_draft(world)
    assert run_main("create") == 0
    assert len(world.calls("gh.repo")) == 1
    assert scan_config(world) == full_config()


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_create_with_an_unreadable_base_repository_uses_the_full_config(
    world: World, stdout: str
) -> None:
    answer: dict[str, object] = {"rc": 0, "stdout": stdout, "stderr": ""}
    world.fake("gh", stdout="u\n", cases={"repo view": answer})
    write_draft(world)
    assert run_main("create") == 0
    assert len(world.calls("gh.repo")) == 1
    assert scan_config(world) == full_config()


@pytest.mark.parametrize(("stdout", "rc", "reduced"), VISIBILITIES)
def test_edit_reads_the_prs_repository_once_and_picks_the_config(
    world: World, stdout: str, rc: int, reduced: bool
) -> None:
    world.fake("gh", stdout="u\n", cases=edit_cases(pr_page(TARGET), stdout, rc))
    write_draft(world)
    assert run_main("edit", str(pr.DRAFT), "--pr", "7") == 0
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", TARGET, *VISIBILITY_READ]
    assert (scan_config(world) != full_config()) is reduced


def test_a_private_working_directory_posting_to_a_public_repo_gets_the_full_config(
    world: World,
) -> None:
    other = "tbhb-dev/elsewhere"
    cases = edit_cases(pr_page(other))
    cases[f"repo view {other}"] = {"rc": 0, "stdout": "PUBLIC\n", "stderr": ""}
    world.fake("gh", stdout="u\n", cases=cases)
    write_draft(world)
    assert run_main("edit", str(pr.DRAFT), "--pr", "7") == 0
    (read,) = world.calls("gh.repo")
    assert read["argv"] == ["repo", "view", other, *VISIBILITY_READ]
    assert scan_config(world) == full_config()


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        7,
        "not a url",
        "https://github.com/o/n/pull/x",
        "http://github.com/o/n/pull/1",
        "https://gitlab.com/o/n/pull/1",
        "https://github.com/o/n/issues/1",
    ],
)
def test_edit_with_an_unresolved_target_refuses_to_post(
    world: World, url: object
) -> None:
    world.fake("gh", stdout="u\n", cases=edit_cases(url, "PRIVATE\n"))
    write_draft(world)
    with pytest.raises(SystemExit, match="cannot determine its GitHub repository"):
        run_main("edit", str(pr.DRAFT), "--pr", "7")
    (view,) = world.calls("gh")
    assert view["argv"] == ["pr", "view", "7", "--json", "body,url"]
    assert world.calls("gh.repo") == []
    assert world.calls("gitleaks") == []


def test_edit_requires_a_draft(world: World) -> None:
    write_draft(world)
    with pytest.raises(SystemExit) as info:
        run_main("edit", "--pr", "7")
    assert info.value.code == 2


def test_edit_requires_a_pr_number(world: World) -> None:
    del world
    with pytest.raises(SystemExit) as info:
        run_main("edit", str(pr.DRAFT))
    assert info.value.code == 2


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as info:
        run_main()
    assert info.value.code == 2


def test_main_defaults_to_the_process_environment(
    world: World, monkeypatch: pytest.MonkeyPatch, agent: dict[str, str]
) -> None:
    write_draft(world)
    for key, val in agent.items():
        monkeypatch.setenv(key, val)
    assert pr.main(["create", "--dry-run"]) == 0
    assert pr.main(["check"]) == 0


def test_create_with_an_unreadable_path(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main("create", str(world.root)) == 1
    assert "cannot read the draft: Is a directory" in capsys.readouterr().out


def test_gh_args_without_metadata(world: World) -> None:
    del world
    assert pr.gh_args({}) == ["--base", "main"]
    assert pr.gh_args({"draft": False}) == ["--base", "main"]


def test_run_gives_the_child_no_terminal(world: World) -> None:
    del world
    done = pr.run(
        [
            str(Path(sys.executable)),
            "-c",
            "import sys;print(sys.stdin.isatty(), repr(sys.stdin.read()))",
        ]
    )
    assert done.stdout == "False ''\n"
    done = pr.run(
        [sys.executable, "-c", "import sys;print(sys.stdin.read())"], stdin="hi"
    )
    assert done.stdout == "hi\n"


def test_run_uses_the_given_directory(tmp_path: Path) -> None:
    done = pr.run([sys.executable, "-c", "import os;print(os.getcwd())"], cwd=tmp_path)
    assert done.stdout.strip() == str(tmp_path.resolve())


def test_runs_as_tbhbdev_alpha_pr(world: World) -> None:
    write_draft(world)
    (world.bin / "bazelisk").unlink()
    world.fake("bash", rc=1, stderr="missing counter")
    bare = world.root.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    world.git("push", "-q", str(bare), "HEAD:refs/heads/main")
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath(SRC),
        # The fetch of the base branch reaches this local repository.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{bare}.insteadOf",
        "GIT_CONFIG_VALUE_0": ORIGIN,
    }
    env.pop("MUTANT_UNDER_TEST", None)
    done = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "alpha", "pr", "check"],
        cwd=world.root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert (done.returncode, done.stderr) == (
        0,
        "size count unavailable: missing counter\n",
    )


# ------------------------------------------------------- mutation-killing pins


def test_sections_without_a_word_limit(world: World) -> None:
    (world.root / pr.SECTIONS).write_text(
        '[[section]]\nheading = "A"\nrequired = true\n\n'
        '[[section]]\nheading = "B"\nrequired = false\nmax_words = 5\n'
    )
    assert pr.load_sections() == [
        pr.Section("A", required=True, max_words=None),
        pr.Section("B", required=False, max_words=5),
    ]


def test_unknown_section_does_not_hide_later_problems() -> None:
    text = "# fix: x\n\n## Nope\n\nx\n\n## Why\n\n"
    got = structure(text)
    known = (
        "Why, Behavior change, Decisions, Deviations from the task, "
        "Needs human judgment, Risk, Verification, Human steering"
    )
    assert (3, f"unknown section 'Nope'; use: {known}") in got
    assert (7, "section 'Why' is empty") in got


def test_code_lines_do_not_hide_later_trailers() -> None:
    text = BASE.replace("v\n", "v\n\n```\ncode\n```\n\nGenerated-by: LLM x\n")
    assert (
        19,
        "the script writes Generated-by and Assisted-by; remove this line",
    ) in structure(text)


def test_trailer_right_after_the_title_is_found() -> None:
    text = "# fix: x\nGenerated-by: LLM x\n\n## Why\n\nw\n"
    assert (
        2,
        "the script writes Generated-by and Assisted-by; remove this line",
    ) in structure(text)


def test_a_non_title_first_line_is_reported_where_it_is() -> None:
    assert (
        2,
        "the first line after the frontmatter must be the H1 title",
    ) in structure("\nplain\n")


def test_quoted_frontmatter_key_points_at_line_one() -> None:
    # The key is not found by a plain prefix, so the error falls back to line 1,
    # even though the same key appears as plain text further down.
    text = '---\n"base": 3\n---\n' + BASE + "\nbase: x\n"
    found = [line for line, why in structure(text) if why.startswith("frontmatter")]
    assert found == [1]


def test_body_of_keeps_spaces_and_edge_letters() -> None:
    assert pr.body_of(["# T", "", "## Why", "x  ", ""]) == "## Why\nx  \n"
    assert pr.body_of(["# T", "", "Xylophone X", ""]) == "Xylophone X\n"
    assert pr.body_of(["# T", "## Why", "x"]) == "## Why\nx\n"


def test_identity_key_splits_on_the_first_equals() -> None:
    value = "LLM harness=claude-code:1;model=a=b;session=s"
    assert pr.identity_key(value) == ("claude-code", "a=b", "s", "")


def test_posting_agent_is_added_to_an_empty_branch(
    world: World, agent: dict[str, str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    got = pr.description(checked_for(), agent, pr.branch_trailers("main"))
    assert got.endswith(f"v\n\nAssisted-by: {POSTING}\n")


def test_a_posting_value_without_a_model_is_rejected(
    world: World, agent: dict[str, str]
) -> None:
    seed_session(world, None)
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    posting = POSTING.replace("model=anthropic:claude-opus-5-5;", "")
    assert pr.posting_value(agent) == posting
    with pytest.raises(SystemExit) as info:
        pr.description(checked_for(), agent, pr.branch_trailers("main"))
    assert str(info.value) == f"invalid LLM trailer: {posting}: model required"


def test_a_bad_branch_trailer_and_posting_value_are_both_listed(
    world: World, agent: dict[str, str]
) -> None:
    seed_session(world, None)
    world.git("switch", "-q", "-c", "topic")
    world.commit("one\n\nGenerated-by: LLM harness=nope")
    posting = POSTING.replace("model=anthropic:claude-opus-5-5;", "")
    with pytest.raises(SystemExit) as info:
        pr.description(checked_for(), agent, pr.branch_trailers("main"))
    assert str(info.value) == (
        "invalid LLM trailer: LLM harness=nope: unknown harness nope; "
        f"{posting}: model required"
    )


@pytest.mark.parametrize("missing", [pr.SCHEMA, pr.SECTIONS])
@pytest.mark.parametrize(
    "argv", [["check"], ["create", "--dry-run"], ["edit", str(pr.DRAFT), "--pr", "3"]]
)
def test_a_missing_baseline_file_names_it(
    world: World, missing: Path, argv: list[str]
) -> None:
    write_draft(world)
    (world.root / missing).rename(world.root.parent / missing.name)
    with pytest.raises(SystemExit) as info:
        run_main(*argv)
    assert info.value.code == (
        f"{missing} is missing: this repository needs the shared baseline"
        " from tbhb-dev/rules"
    )


def test_a_missing_baseline_file_exits_without_a_traceback(world: World) -> None:
    write_draft(world)
    (world.root / pr.SECTIONS).rename(world.root.parent / "sections.toml")
    env = {**os.environ, "PYTHONPATH": pythonpath(SRC)}
    env.pop("MUTANT_UNDER_TEST", None)
    done = subprocess.run(
        [sys.executable, "-m", "tbhbdev", "alpha", "pr", "check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=world.root,
        env=env,
    )
    assert (done.returncode, done.stdout) == (1, "")
    assert done.stderr == (
        f"{pr.SECTIONS} is missing: this repository needs the shared baseline"
        " from tbhb-dev/rules\n"
    )


def test_every_invalid_trailer_is_listed(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(
        "one\n\nGenerated-by: LLM harness=nope\nAssisted-by: LLM harness=nope2"
    )
    with pytest.raises(SystemExit) as info:
        pr.description(checked_for(), {}, pr.branch_trailers("main"))
    assert "; " in str(info.value)
    assert str(info.value).count("LLM harness=") == 2


@pytest.mark.usefixtures("world")
def test_branch_git_calls_run_in_the_current_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path | None] = []
    real = pr.git

    def spy(*args: str, cwd: Path | None = None) -> tuple[int, str]:
        seen.append(cwd)
        return real(*args, cwd=cwd)

    monkeypatch.setattr(pr, "git", spy)
    pr.branch_trailers("main")
    assert seen == [None, None]


def test_help_describes_the_script(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_main("--help")
    assert "\n\nCheck a PR draft and open the PR.\n\n" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["edit"])
def test_pr_number_must_be_an_integer(command: str) -> None:
    with pytest.raises(SystemExit) as info:
        run_main(command, str(pr.DRAFT), "--pr", "seven")
    assert info.value.code == 2


# ---------------------------------------------------------------------- hook

PATCH = "*** Begin Patch\n*** {verb}: {path}\n*** End Patch\n"
DRAFT_REL = ".github/PULL_REQUEST_DRAFT.md"
LINT_PROBLEM = "pull request hook skipped"


def run_hook(monkeypatch: pytest.MonkeyPatch, harness: str, payload: object) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    return pr.main(["hook", harness], {})


def hook_payloads(root: Path) -> dict[str, tuple[str, dict[str, object]]]:
    """(harness, payload) pairs that all write the draft."""
    sub = root / "sub"
    absolute = str(root / DRAFT_REL)
    return {
        "claude-relative": (
            "claude",
            {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(root)},
        ),
        "claude-absolute": (
            "claude",
            {"tool_input": {"file_path": absolute}, "cwd": str(sub)},
        ),
        "claude-from-subdir": (
            "claude",
            {"tool_input": {"file_path": f"../{DRAFT_REL}"}, "cwd": str(sub)},
        ),
        "claude-no-cwd": ("claude", {"tool_input": {"file_path": absolute}}),
        "codex-add": (
            "codex",
            {
                "tool_name": "apply_patch",
                "cwd": str(root),
                "tool_input": {
                    "command": PATCH.format(verb="Add File", path=DRAFT_REL)
                },
            },
        ),
        "codex-move": (
            "codex",
            {
                "cwd": str(sub),
                "tool_input": {
                    "command": PATCH.format(verb="Update File", path="a.md")
                    + f"*** Move to: ../{DRAFT_REL}\n"
                },
            },
        ),
        "agy-dict": (
            "antigravity",
            {
                "cwd": str(root),
                "toolCall": {"name": "write_to_file", "args": {"TargetFile": absolute}},
                "error": "",
            },
        ),
        "agy-json": (
            "antigravity",
            {
                "cwd": str(root),
                "toolName": "replace_file_content",
                "toolInput": json.dumps({"AbsolutePath": absolute}),
            },
        ),
    }


HOOK_NAMES = list(hook_payloads(Path("/r")))


@pytest.fixture
def hooked(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root)
    return world


def write_hook_draft(world: World, text: str) -> None:
    (world.root / DRAFT_REL).write_text(text)


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hook_clean_draft_is_silent(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())[name]
    if name == "claude-no-cwd":
        monkeypatch.chdir(hooked.root / "sub")
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert len(hooked.calls("vale")) == 1


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_hook_feeds_problems_back_on_stderr(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    write_hook_draft(hooked, "## Why\n\nw\n")
    harness, payload = hook_payloads(hooked.root.resolve())[name]
    if name == "claude-no-cwd":
        monkeypatch.chdir(hooked.root / "sub")
    assert run_hook(monkeypatch, harness, payload) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"{DRAFT_REL} has problems. Fix them and save the file again.\n"
        f"{DRAFT_REL}:1: the first line after the frontmatter must be the H1 title\n"
    )


def test_hook_outside_tbhb_dev_is_silent(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hooked.git("remote", "set-url", "origin", "git@github.com:someone/x.git")
    write_hook_draft(hooked, "broken")
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    assert capsys.readouterr() == ("", "")
    assert hooked.calls("vale") == []


def test_hook_checks_the_draft_of_the_payloads_worktree(
    hooked: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each worktree has its own draft; the hook checks the one it wrote."""
    hooked.git("add", "-A")
    hooked.commit("base")
    tree = tmp_path / "tree"
    hooked.git("worktree", "add", "-q", "-b", "tree", str(tree))
    write_hook_draft(hooked, BASE)
    (tree / DRAFT_REL).write_text("## Why\n\nw\n")
    payload = {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(tree)}
    assert run_hook(monkeypatch, "claude", payload) == 2
    assert capsys.readouterr().err.startswith(f"{DRAFT_REL} has problems.")
    assert Path.cwd() == tree.resolve()
    payload = {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(hooked.root)}
    assert run_hook(monkeypatch, "claude", payload) == 0
    assert capsys.readouterr() == ("", "")


def test_hook_checks_the_payloads_repository_from_anywhere(
    hooked: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The origin check reads the payload's cwd, not the process's."""
    write_hook_draft(hooked, "## Why\n\nw\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    payload = {"tool_input": {"file_path": DRAFT_REL}, "cwd": str(hooked.root)}
    assert run_hook(monkeypatch, "claude", payload) == 2
    assert capsys.readouterr().err.startswith(f"{DRAFT_REL} has problems.")


def test_root_outside_a_repository_is_the_directory_given(
    hooked: World, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert Path.cwd() == hooked.root
    assert pr.root(elsewhere) == elsewhere


def test_hook_reports_lint_findings_with_draft_lines(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hooked.fake("cspell", rc=1, stdout=f"{DRAFT_REL}:3:1 - Unknown word (zz)\n")
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 2
    assert f"{DRAFT_REL}:3: cspell: Unknown word (zz)\n" in capsys.readouterr().err


UNRELATED: list[tuple[str, dict[str, object]]] = [
    ("claude", {"tool_input": {"file_path": "README.md"}}),
    ("claude", {"tool_input": {"file_path": "sub/.github/PULL_REQUEST_DRAFT.md"}}),
    ("claude", {"tool_input": {"file_path": ""}}),
    ("claude", {"tool_input": {"file_path": 3}}),
    ("claude", {"tool_input": {"command": "ls"}}),
    ("claude", {}),
    ("codex", {"tool_input": {"command": "ls .github"}}),
    ("codex", {"tool_input": {"command": 7}}),
    (
        "codex",
        {"tool_input": {"command": PATCH.format(verb="Add File", path="a.md")}},
    ),
    (
        "antigravity",
        {"toolCall": {"name": "run_command", "args": {"TargetFile": DRAFT_REL}}},
    ),
    (
        "antigravity",
        {"toolCall": {"name": "write_file", "args": {"TargetFile": "a.md"}}},
    ),
    ("antigravity", {"toolCall": {"name": "write_file", "args": "not json"}}),
    ("antigravity", {}),
    # A Claude-shaped payload to another harness touches nothing.
    ("codex", {"tool_input": {"file_path": DRAFT_REL}}),
    ("antigravity", {"tool_input": {"file_path": DRAFT_REL}}),
]


@pytest.mark.parametrize(("harness", "payload"), UNRELATED)
def test_hook_ignores_other_tool_calls(
    hooked: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: str,
    payload: dict[str, object],
) -> None:
    write_hook_draft(hooked, "broken")
    payload = {"cwd": str(hooked.root.resolve()), **payload}
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    for name in ("vale", "cspell", "rumdl", "cog"):
        assert hooked.calls(name) == []


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", "null"])
def test_hook_ignores_unreadable_payloads(
    hooked: World, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    write_hook_draft(hooked, "broken")
    assert run_hook(monkeypatch, "claude", stdin) == 0
    assert hooked.calls("vale") == []


def test_hook_with_a_missing_draft_file_reports_it(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 2
    assert (
        f"{DRAFT_REL}:1: cannot read the draft: No such file or directory\n"
        in capsys.readouterr().err
    )


def test_hook_fails_open_when_a_tool_is_missing(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (hooked.bin / "vale").unlink()
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"{LINT_PROBLEM}: vale not found")


def boom(*_args: object, **_kwargs: object) -> pr.Checked:
    message = "boom"
    raise RuntimeError(message)


def test_hook_fails_open_on_a_crash(
    hooked: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pr, "load_checked", boom)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0
    assert capsys.readouterr().err == f"{LINT_PROBLEM}: boom\n"


def test_hook_rejects_an_unknown_harness(hooked: World) -> None:
    del hooked
    with pytest.raises(SystemExit) as info:
        pr.main(["hook", "vim"], {})
    assert info.value.code == 2


def test_hook_ignores_trailers_and_the_branch(
    hooked: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooked.git("switch", "-q", "-c", "topic")
    hooked.commit("one\n\nGenerated-by: LLM nonsense")
    write_hook_draft(hooked, BASE)
    harness, payload = hook_payloads(hooked.root.resolve())["claude-relative"]
    assert run_hook(monkeypatch, harness, payload) == 0


# ------------------------------------------------------------ frontmatter parse


def test_parse_frontmatter_splits_block_and_body() -> None:
    assert pr.parse_frontmatter("---\nbase: dev\n---\nbody\n") == (
        {"base": "dev"},
        "body\n",
    )
    assert pr.parse_frontmatter("---\nbase: dev\n---  \nbody") == (
        {"base": "dev"},
        "body",
    )
    assert pr.parse_frontmatter("---\n---\nbody") == (None, "body")
    assert pr.parse_frontmatter("plain\n") == (None, "plain\n")


def test_parse_frontmatter_keeps_dates_as_text() -> None:
    data, _ = pr.parse_frontmatter("---\nmilestone: 2024-01-02\n---\n")
    assert data == {"milestone": "2024-01-02"}


def test_parse_frontmatter_errors() -> None:
    with pytest.raises(pr.FrontmatterError, match=r"^unterminated frontmatter$"):
        pr.parse_frontmatter("---\nbase: x\n")
    with pytest.raises(pr.FrontmatterError) as info:
        pr.parse_frontmatter("---\nbase: [\n---\n")
    message = str(info.value)
    assert message.startswith("invalid YAML: ")
    assert "\n" not in message


def test_cog_runs_in_the_worktree_top_from_a_subdirectory(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    (world.root / "sub").mkdir()
    monkeypatch.chdir(world.root / "sub")
    assert pr.title_problems("fix: x") == []
    assert world.calls("cog")[-1]["cwd"] == str(world.root.resolve())


@pytest.mark.usefixtures("world")
def test_branch_trailers_come_from_the_current_repository_not_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trailers come from the repository the command runs in."""
    other = tmp_path / "other"
    other.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "commit.gpgsign", "false"),
        ("commit", "--allow-empty", "-q", "-m", "init"),
        ("update-ref", "refs/remotes/origin/main", "HEAD"),
        ("commit", "--allow-empty", "-q", "-m", f"feat: x\n\nGenerated-by: {FALLBACK}"),
    ):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    monkeypatch.chdir(other)
    assert pr.root() == other.resolve()
    assert pr.branch_trailers("main") == [("Generated-by", FALLBACK)]


def test_same_identity_in_commit_and_posting_is_one_generated_by() -> None:
    """Generated-by wins over the posting agent's own Assisted-by."""
    assert pr.merge_trailers([("Generated-by", FALLBACK)], FALLBACK) == [
        ("Generated-by", FALLBACK)
    ]


def test_generated_by_precedes_assisted_by() -> None:
    assert pr.merge_trailers([("Assisted-by", OTHER), ("Generated-by", GEN)], None) == [
        ("Generated-by", GEN),
        ("Assisted-by", OTHER),
    ]


def test_description_for_one_identity_ends_in_one_generated_by(
    world: World, agent: dict[str, str]
) -> None:
    """Regression: a branch commit and the posting agent share an identity."""
    posting = pr.posting_value(agent)
    assert posting is not None
    world.git("update-ref", "refs/remotes/origin/main", "HEAD")
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"feat: x\n\nGenerated-by: {posting}")
    checked = pr.check_text(BASE)
    body = pr.description(checked, agent, pr.branch_trailers("main"))
    assert body.endswith(f"\nGenerated-by: {posting}\n")
    assert "Assisted-by" not in body


@pytest.mark.parametrize(
    ("body", "lines"),
    [
        ("One line.\n\nNext paragraph.\n", []),
        ("First line\nsecond line\n", [1]),
        ("a\nb\nc\n\nd\ne\n", [1, 5]),
        ("- item one\n- item two\n", []),
        ("- item one\n  continues\n- item two\n", [1]),
        ("1. one\n2) two\n", []),
        ("Intro:\n- a\n- b\n", []),
        ("text\n```\ncode\nmore\n```\ntail\n", []),
        ("| a | b |\n| - | - |\n| 1 | 2 |\n", []),
        ("### Heading\ntext\n", []),
        ("text\n<!-- note -->\nmore\n", []),
        (f"text\n{GEN_LINE}\n", []),
        ("text\nSigned-off-by: A <a@b.c>\n", []),
    ],
)
def test_wrap_problems(body: str, lines: list[int]) -> None:
    scanned, _ = pr.scan(body.splitlines(), 1)
    assert pr.wrap_problems(scanned) == [(no, pr.WRAPPED) for no in lines]


def ref(no: int, number: int) -> pr.Problem:
    return (no, pr.BARE_REF_MESSAGE.format(number=number))


def test_the_bare_reference_message_names_the_fix() -> None:
    assert pr.BARE_REF_MESSAGE.format(number=7) == (
        "#7 links to this repository's #7; write owner/repo#7, even for this repository"
    )


@pytest.mark.parametrize(
    ("body", "problems"),
    [
        ("See #12.\n", [ref(1, 12)]),
        ("(#3) and #4\n", [ref(1, 3), ref(1, 4)]),
        ("- item #7\n", [ref(1, 7)]),
        ("tbhb-dev/tools#4 and tbhb-dev/renovate-config#2\n", []),
        ("x.y#5 and a-b#6 and &#38; and path/#8\n", []),
        ("`#9` in code\n", []),
        ("```\n#10\n```\n", []),
        ("[#11](https://example.com/11) and [#12][r]\n", []),
        ("https://github.com/o/r/pull/13#14\n", []),
        ("<!-- #15 -->\n", []),
        ("## Why\n#16 here\n", [ref(2, 16)]),
        ("issue#17 and #x and #\n", []),
    ],
)
def test_ref_problems(body: str, problems: list[pr.Problem]) -> None:
    scanned, _ = pr.scan(body.splitlines(), 1)
    assert pr.ref_problems(scanned) == problems


def test_a_bare_reference_fails_the_check_in_the_title_and_body() -> None:
    text = BASE.replace("# fix: x\n", "# fix: x (#1)\n").replace("\nw\n", "\nsee #2\n")
    found = structure(text)
    assert ref(1, 1) in found
    assert ref(5, 2) in found


def test_the_passing_draft_has_no_bare_references() -> None:
    _, body = pr.parse_frontmatter(FIXTURE.read_text())
    scanned, _ = pr.scan(body.splitlines(), 1)
    assert pr.ref_problems(scanned) == []


def test_a_wrapped_draft_fails_the_check() -> None:
    text = BASE.replace("\nw\n", "\nline one\nline two\n")
    assert (5, pr.WRAPPED) in structure(text)


def test_the_passing_draft_has_no_wrapped_paragraphs() -> None:
    _, body = pr.parse_frontmatter(FIXTURE.read_text())
    scanned, _ = pr.scan(body.splitlines(), 1)
    assert pr.wrap_problems(scanned) == []


def test_rumdl_line_length_is_off_for_drafts() -> None:
    assert (
        "MD013" in (REAL / ".rumdl.toml").read_text().split("disable")[1].split("]")[0]
    )


# ------------------------------------------------------------ opened by


def test_create_writes_opened_by_first_and_the_ledger(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit(f"one\n\nGenerated-by: {GEN}")
    write_draft(world)
    assert run_main("create", env=agent) == 0
    _, call = world.calls("gh")
    assert str(call["stdin"]).endswith(
        f"\nv\n\n{OPENER}\nGenerated-by: {GEN}\nAssisted-by: {POSTING}\n"
    )
    assert "subagent=" not in OPENER
    path = ledgers(world) / f"{agent_identity.hx(ROOT)}.json"
    assert json.loads(path.read_text()) == {"prs": ["tbhb-dev/tools#1"]}
    assert capsys.readouterr() == ("https://github.com/tbhb-dev/tools/pull/1\n", "")
    world.fake("gh", stdout="Created\nhttps://github.com/tbhb-dev/tools/pull/12\n")
    assert run_main("create", env=agent) == 0
    assert json.loads(path.read_text()) == {
        "prs": ["tbhb-dev/tools#1", "tbhb-dev/tools#12"]
    }


@pytest.mark.parametrize("site", ["opener", "create-description", "edit-description"])
def test_pr_commands_report_unresolved_attribution(
    world: World,
    agent: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    site: str,
) -> None:
    write_draft(world)
    agent_identity.record_command(ROOT, SUBAGENT, "background command", world.root)
    if site == "create-description":
        monkeypatch.setattr(pr, "opened_by", Mock(return_value=(None, None)))
    argv = (
        ("edit", str(pr.DRAFT), "--pr", "1")
        if site == "edit-description"
        else ("create",)
    )
    assert run_main(*argv, env=agent) == 1
    assert capsys.readouterr() == (
        "",
        (
            "tbhbdev alpha pr: Attribution couldn't be resolved. Retry the pull "
            "request command from the main session after the subagent's command "
            "finishes and its session record is available.\n"
        ),
    )
    reads = [call["argv"] for call in world.calls("gh")]
    if site == "edit-description":
        assert reads == [
            ["pr", "view", "1", "--json", "body,url"],
            [*PR_COMMITS_CALL, f"repos/{TARGET}/pulls/1/commits"],
        ]
    else:
        assert reads == []


def test_a_subagent_opens_under_its_root(world: World, agent: dict[str, str]) -> None:
    write_draft(world)
    child = agent_identity.sha256_64("agent-raw")
    agent_identity.write_user_record(child, {"root": ROOT, "model": "claude-x"})
    assert run_main("create", env={**agent, "ASCM_AGENT_ID": child}) == 0
    _, call = world.calls("gh")
    assert str(call["stdin"]).endswith(
        f"\n\n{OPENER}\nAssisted-by: LLM harness=claude-code:2.1.0;"
        f"model=anthropic:claude-x;session={ROOT};subagent={child};surface=cli\n"
    )
    assert opened.read_ledger(ROOT) == {"tbhb-dev/tools#1"}


@pytest.mark.parametrize(
    "env", [{}, {"ANTIGRAVITY_CONVERSATION_ID": "conv-raw"}], ids=["human", "agy"]
)
def test_no_root_means_no_line_and_no_ledger(
    world: World, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    write_draft(world)
    conv = agent_identity.sha256_64("conv-raw")
    agent_identity.write_user_record(conv, {"model": "gemini-x"})
    assert run_main("create", env=env) == 0
    _, call = world.calls("gh")
    assert "Opened-by" not in str(call["stdin"])
    assert not ledgers(world).exists()
    expected = (
        "warning: no Opened-by line or ledger entry: cannot resolve the root session\n"
        if env
        else ""
    )
    assert capsys.readouterr().err == expected


def test_a_failed_create_records_nothing(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", rc=1, stdout="https://github.com/tbhb-dev/example/pull/1\n")
    write_draft(world)
    assert run_main("create", env=agent) == 1
    assert not ledgers(world).exists()
    assert capsys.readouterr().err == ("https://github.com/tbhb-dev/example/pull/1\n")


def test_create_without_a_url_records_nothing(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", stdout="https://github.com/tbhb-dev/tools/issues/1\n")
    write_draft(world)
    assert run_main("create", env=agent) == 1
    assert capsys.readouterr().err == (
        "gh printed no pull request URL; nothing was recorded\n"
    )
    assert not ledgers(world).exists()


def test_create_with_a_corrupt_ledger_reports_it(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path = ledgers(world) / f"{agent_identity.hx(ROOT)}.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    write_draft(world)
    assert run_main("create", env=agent) == 0
    assert capsys.readouterr().err == (
        "warning: the PR is open, but the ledger was not updated: "
        f"{path} holds no list of pull requests\n"
    )
    assert path.read_text() == "[]"


LIVE = "Opened-by: LLM harness=codex;session=sha-256-64:cccccccccccccccc"


def live_body(body: object, rc: int = 0, stderr: str = "") -> dict[str, object]:
    """The fake gh answer to ``gh pr view N --json body,url``."""
    if rc == 0:
        return view_case(pr_page(TARGET), body)
    return {"rc": rc, "stdout": "", "stderr": stderr}


@pytest.mark.parametrize(
    ("body", "kept"),
    [
        (f"## Why\n\nw\n\n{LIVE}\n{GEN_LINE}\n", LIVE),
        (f"## Why\r\n\r\nw\r\n\r\n{LIVE}\r\n{GEN_LINE}\r\n\r\n  \n", LIVE),
        (f"{GEN_LINE}\n{LIVE}\n", LIVE),
        (f"## Why\n\n{LIVE}\n\nw\n\n{GEN_LINE}\n", None),
        (f"## Why\n\nw\n\n{GEN_LINE}\n", None),
        (f"## Why\n\nw\n\n {LIVE}\n", None),
        ("", None),
        (None, None),
    ],
)
def test_edit_copies_the_live_opened_by_line(
    world: World, agent: dict[str, str], body: object, kept: str | None
) -> None:
    world.fake("gh", cases={"pr view": live_body(body)})
    write_draft(world)
    assert run_main("edit", str(pr.DRAFT), "--pr", "7", env=agent) == 0
    view, _, edit = world.calls("gh")
    assert view["argv"] == ["pr", "view", "7", "--json", "body,url"]
    assert cast("list[str]", edit["argv"])[:3] == ["pr", "edit", "7"]
    opened_by = f"{kept}\n" if kept else ""
    assert str(edit["stdin"]).endswith(f"\nv\n\n{opened_by}Assisted-by: {POSTING}\n")
    assert not ledgers(world).exists()


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (live_body("", rc=1, stderr="no pull\nrequest"), "no pull request"),
        ({"rc": 0, "stdout": "not json", "stderr": ""}, "not json"),
        ({"rc": 0, "stdout": "[]", "stderr": ""}, "[]"),
    ],
)
def test_edit_stops_when_it_cant_read_the_live_body(
    world: World, answer: dict[str, object], why: str
) -> None:
    world.fake("gh", cases={"pr view": answer})
    write_draft(world)
    message = f"cannot read the body of PR 7 to keep its Opened-by: {why}"
    with pytest.raises(SystemExit, match=f"^{re.escape(message)}$"):
        run_main("edit", str(pr.DRAFT), "--pr", "7")
    argvs = [cast("list[str]", call["argv"]) for call in world.calls("gh")]
    assert [argv[:2] for argv in argvs] == [["pr", "view"]]


@pytest.mark.parametrize(
    "line",
    [
        "Opened-by: LLM harness=codex",
        "opened-by: x",
        "  Opened-By : x",
        "OPENED-BY:",
    ],
)
def test_check_rejects_an_opened_by_line(line: str) -> None:
    assert structure(BASE + f"\n{line}\n") == [(15, pr.OPENED_MESSAGE)]


def test_check_rejects_opened_by_in_code_too() -> None:
    text = BASE + "\n```text\nOpened-by: x\n```\n"
    assert structure(text) == [(16, pr.OPENED_MESSAGE)]
    assert structure(BASE + "\nSee the Opened-by: line.\n") == []


R1 = agent_identity.sha256_64("r1")
R2 = agent_identity.sha256_64("r2")
R3 = agent_identity.sha256_64("r3")
SUB = agent_identity.sha256_64("sub")


def gh_list(items: list[dict[str, object]], rc: int = 0) -> dict[str, object]:
    return {"rc": rc, "stdout": json.dumps(items), "stderr": ""}


BACKFILL_CASES: dict[str, dict[str, object]] = {
    "repo list tbhb-dev": gh_list(
        [{"nameWithOwner": "tbhb-dev/b"}, {"nameWithOwner": "tbhb-dev/a"}]
    ),
    "pr list -R tbhb-dev/a": gh_list(
        [
            {
                "number": 1,
                "body": (
                    f"x\n\nGenerated-by: LLM harness=claude-code;session={R1}\n"
                    f"Assisted-by: LLM harness=codex;session={R2};subagent={SUB}\n"
                ),
            },
            {"number": 2, "body": f"Opened-by: LLM harness=codex;session={R3}\n"},
            {"number": 3, "body": None},
            {
                "number": 4,
                "body": (
                    f"session={R1}\nGenerated-by: LLM xsession={R2}\n"
                    f"Generated-by: LLM session={R3}x\nGenerated-by: x session={R3}\n"
                    f"Reviewed-by: LLM session={R3}\n"
                ),
            },
        ]
    ),
    "pr list -R tbhb-dev/b": gh_list(
        [{"number": 5, "body": f"  Generated-by:  LLM model=m;session={R1}  \n"}]
    ),
}


def test_backfill_records_every_session_on_every_pr(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", cases=BACKFILL_CASES)
    for _ in range(2):
        assert run_main("backfill-opened", "tbhb-dev") == 0
        assert capsys.readouterr().out == (
            "tbhb-dev/a: 2 of 4 pull requests recorded\n"
            "tbhb-dev/b: 1 of 1 pull requests recorded\n"
        )
        assert opened.read_ledger(R1) == {"tbhb-dev/a#1", "tbhb-dev/b#5"}
        assert opened.read_ledger(R2) == {"tbhb-dev/a#1"}
        assert opened.read_ledger(R3) == {"tbhb-dev/a#2"}
        assert sorted(p.name for p in ledgers(world).glob("*.json")) == sorted(
            f"{agent_identity.hx(r)}.json" for r in (R1, R2, R3)
        )
    calls = [call["argv"] for call in world.calls("gh")][:3]
    assert calls == [
        ["repo", "list", "tbhb-dev", "--limit", "1000", "--json", "nameWithOwner"],
        ["pr", "list", "-R", "tbhb-dev/a", "--state", "all", "--limit", "10000",
         "--json", "number,body"],
        ["pr", "list", "-R", "tbhb-dev/b", "--state", "all", "--limit", "10000",
         "--json", "number,body"],
    ]  # fmt: skip


def test_backfill_with_no_repositories_prints_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", stdout="[]")
    assert run_main("backfill-opened", "nobody") == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        ({"rc": 1, "stdout": "", "stderr": "HTTP\n404"}, "HTTP 404"),
        ({"rc": 1, "stdout": "partial", "stderr": ""}, "partial"),
        ({"rc": 0, "stdout": "{}", "stderr": ""}, "{}"),
        ({"rc": 0, "stdout": "nope", "stderr": ""}, "nope"),
    ],
)
def test_backfill_stops_when_gh_fails(
    world: World, answer: dict[str, object], why: str
) -> None:
    world.fake("gh", cases={"repo list": answer})
    with pytest.raises(SystemExit, match=f"^gh repo list failed: {why}$"):
        run_main("backfill-opened", "tbhb-dev")


def test_backfill_stops_at_a_corrupt_ledger(world: World) -> None:
    world.fake("gh", cases=BACKFILL_CASES)
    path = ledgers(world) / f"{agent_identity.hx(R1)}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    with pytest.raises(SystemExit, match=f"^{re.escape(str(path))} is not JSON$"):
        run_main("backfill-opened", "tbhb-dev")
    assert path.read_text() == "{"


@pytest.mark.parametrize("rc", [0, 3])
def test_rerun_retries_failed_jobs_of_the_current_head(
    world: World, capsys: pytest.CaptureFixture[str], rc: int
) -> None:
    head = "a" * 40
    world.fake(
        "gh",
        rc=rc,
        stdout="out\n",
        stderr="err\n",
        cases={
            "pr view": {
                "rc": 0,
                "stdout": json.dumps({"headRefOid": head}),
                "stderr": "",
            },
            "run list": gh_list([{"databaseId": 123, "conclusion": "failure"}]),
        },
    )
    assert run_main("rerun", "45") == rc
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("out\n", "err\n")
    calls = world.calls("gh")
    assert [call["argv"] for call in calls] == [
        ["pr", "view", "45", "--json", "headRefOid"],
        [
            "run",
            "list",
            "--commit",
            head,
            "--limit",
            "1",
            "--json",
            "databaseId,conclusion",
        ],
        ["run", "rerun", "123", "--failed"],
    ]
    assert all(call["stdin"] == "" for call in calls)


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [{"databaseId": 123, "conclusion": "success"}],
        [{"databaseId": 123, "conclusion": ""}],
        [
            {"databaseId": 123, "conclusion": "success"},
            {"databaseId": 122, "conclusion": "failure"},
        ],
    ],
)
def test_rerun_reports_no_failed_run(
    world: World, capsys: pytest.CaptureFixture[str], runs: list[dict[str, object]]
) -> None:
    world.fake(
        "gh",
        cases={
            "pr view": {
                "rc": 0,
                "stdout": json.dumps({"headRefOid": "a" * 40}),
                "stderr": "",
            },
            "run list": gh_list(runs),
        },
    )
    assert run_main("rerun", "45") == 1
    assert capsys.readouterr().err == "no failed run for the current head of PR 45\n"
    assert len(world.calls("gh")) == 2


NOW = dt.datetime(2026, 11, 23, 18, 0, tzinfo=dt.UTC)
HEAD = "a" * 40


class Stuck:
    """A stuck-run scenario: gh's pr view and run list, plus the run helpers."""

    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = world
        self.statuses = ["in_progress", "completed"]
        self.conclusion = "cancelled"
        self.numbers: list[int] = [45]
        self.jobs: list[dict[str, str]] = []
        self.new_head = HEAD
        self.invoked: list[list[str]] = []
        self.pauses: list[float] = []
        self.meta: dict[str, object] = {
            "headRefOid": HEAD,
            "headRefName": "topic",
            "isCrossRepository": False,
        }
        self.runs: list[dict[str, object]] = []
        monkeypatch.setattr(pr, "utcnow", Mock(return_value=NOW))
        monkeypatch.setattr(pr, "run_view", self.view)
        monkeypatch.setattr(pr, "head_of", self.head)
        monkeypatch.setattr(pr, "invoke_gh", self.invoke)
        monkeypatch.setattr(pr, "pause", self.pauses.append)

    def head(self, _number: int) -> str:
        return self.new_head

    def view(self, _run_id: object, field: str) -> dict[str, object]:
        if field == "jobs":
            return {"jobs": self.jobs}
        return {"status": self.statuses.pop(0), "conclusion": self.conclusion}

    def invoke(self, argv: list[str], _body: str) -> tuple[int, str]:
        self.invoked.append(argv[1:])
        return (1 if argv[1:3] == ["run", "cancel"] and self.cancel_fails else 0), ""

    cancel_fails = False

    def start(self) -> None:
        self.world.fake(
            "gh",
            cases={
                "pr view": {"rc": 0, "stdout": json.dumps(self.meta), "stderr": ""},
                "run list": gh_list(self.runs),
                "api": {"rc": 0, "stdout": json.dumps(self.numbers), "stderr": ""},
            },
        )


def old_job(
    minutes: int, name: str = "bazel", status: str = "in_progress"
) -> dict[str, str]:
    started = NOW - dt.timedelta(minutes=minutes)
    return {"name": name, "status": status, "startedAt": started.isoformat()}


def a_run(run_id: int = 7, **fields: object) -> dict[str, object]:
    run_fields: dict[str, object] = {
        "databaseId": run_id,
        "status": "in_progress",
        "headSha": HEAD,
    }
    return run_fields | fields


def test_rerun_stuck_cancels_waits_and_reruns(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(61)]
    scene.statuses = ["in_progress", "in_progress", "completed"]
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 0
    assert scene.invoked == [["run", "cancel", "7"], ["run", "rerun", "7"]]
    assert scene.pauses == [pr.CANCEL_PAUSE, pr.CANCEL_PAUSE]
    argvs = [cast("list[str]", c["argv"]) for c in world.calls("gh")]
    list_call = next(argv for argv in argvs if argv[1] == "list")
    assert list_call[2:8] == [
        "--commit",
        HEAD,
        "--event",
        "pull_request",
        "--branch",
        "topic",
    ]


@pytest.mark.parametrize(
    ("runs", "jobs"),
    [
        ([], [old_job(300)]),
        ([a_run(status="completed")], [old_job(300)]),
        ([a_run(headSha="b" * 40)], [old_job(300)]),
        ([a_run()], [old_job(59)]),
        ([a_run()], [old_job(300, status="queued")]),
    ],
)
def test_rerun_stuck_leaves_a_run_that_is_not_stuck(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runs: list[dict[str, object]],
    jobs: list[dict[str, str]],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = runs
    scene.jobs = jobs
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert capsys.readouterr().err == "no stuck run for the current head of PR 45\n"
    assert scene.invoked == []


def test_rerun_stuck_ignores_a_run_of_another_pull_request(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.numbers = [46]
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert capsys.readouterr().err == "no stuck run for the current head of PR 45\n"
    assert scene.invoked == []


def test_rerun_stuck_skips_a_run_shared_with_another_pull_request(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.numbers = [46, 45]
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert capsys.readouterr().err == (
        "run 7 also belongs to PR 46; skipping\n"
        "no stuck run for the current head of PR 45\n"
    )
    assert scene.invoked == []


def test_rerun_stuck_does_not_rerun_a_run_that_completed_normally(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.conclusion = "success"
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "completed as success; no rerun" in capsys.readouterr().err
    assert scene.invoked == [["run", "cancel", "7"]]


def test_rerun_stuck_skips_a_young_run_to_reach_a_stuck_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run(9), a_run(7)]
    ages = {9: 5, 7: 90}

    def view(run_id: object, field: str) -> dict[str, object]:
        if field == "jobs":
            return {"jobs": [old_job(ages[int(str(run_id))])]}
        return {"status": "completed", "conclusion": "cancelled"}

    monkeypatch.setattr(pr, "run_view", view)
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 0
    assert scene.invoked == [["run", "cancel", "7"], ["run", "rerun", "7"]]


def test_rerun_stuck_refuses_a_fork(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.meta["isCrossRepository"] = True
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "comes from a fork" in capsys.readouterr().err
    assert len(world.calls("gh")) == 1


def test_rerun_stuck_refuses_when_the_head_moved(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.new_head = "c" * 40
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "moved to a new head" in capsys.readouterr().err
    assert scene.invoked == []


def test_rerun_stuck_stops_when_the_cancel_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.cancel_fails = True
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert scene.invoked == [["run", "cancel", "7"]]


def test_rerun_stuck_refuses_when_the_cancel_never_completes(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scene = Stuck(world, monkeypatch)
    scene.runs = [a_run()]
    scene.jobs = [old_job(300)]
    scene.statuses = ["in_progress"] * pr.CANCEL_POLLS
    scene.start()
    assert run_main("rerun", "45", "--stuck") == 1
    assert "did not finish cancelling" in capsys.readouterr().err
    assert scene.invoked == [["run", "cancel", "7"]]
    assert len(scene.pauses) == pr.CANCEL_POLLS


def test_rerun_stuck_reports_an_unreadable_pull_request(world: World) -> None:
    world.fake("gh", rc=1, stderr="gone")
    with pytest.raises(SystemExit, match="cannot read PR 45: gone"):
        run_main("rerun", "45", "--stuck")


def test_run_view_reports_an_unreadable_run(world: World) -> None:
    world.fake("gh", rc=1, stderr="gone")
    with pytest.raises(SystemExit, match="cannot read run 7: gone"):
        pr.run_view(7, "jobs")


def test_run_view_returns_the_mapping_gh_prints(world: World) -> None:
    world.fake("gh", stdout='{"jobs": []}')
    assert pr.run_view(7, "jobs") == {"jobs": []}


def test_pause_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = Mock()
    monkeypatch.setattr(pr.time, "sleep", slept)
    pr.pause(2.5)
    slept.assert_called_once_with(2.5)


def test_utcnow_is_timezone_aware() -> None:
    assert pr.utcnow().tzinfo is dt.UTC


def test_rerun_reports_a_missing_pull_request(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake(
        "gh",
        rc=1,
        stderr="Could not resolve to a PullRequest with the number of 45\n",
    )
    with pytest.raises(SystemExit) as info:
        run_main("rerun", "45")
    assert str(info.value) == (
        "cannot read the head of PR 45: "
        "Could not resolve to a PullRequest with the number of 45"
    )
    assert capsys.readouterr().out == ""
    assert len(world.calls("gh")) == 1


@pytest.mark.parametrize("argv", [("rerun",), ("rerun", "abc")])
def test_rerun_requires_a_numeric_pr_number(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main(*argv)
    assert info.value.code == 2


@pytest.mark.parametrize(
    ("jobs", "expected", "code"),
    [
        (
            [
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "lint: SUCCESS\nunit tests: SUCCESS\n",
            0,
        ),
        (
            [
                {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "lint: FAILURE\nunit tests: SUCCESS\n",
            1,
        ),
        (
            [{"name": "tests", "status": "QUEUED", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "IN_PROGRESS", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "WAITING", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "REQUESTED", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "PENDING", "conclusion": ""}],
            "tests: PENDING\n",
            3,
        ),
        (
            [{"name": "tests", "status": "IN_PROGRESS", "conclusion": None}],
            "tests: PENDING\n",
            3,
        ),
        (
            [
                {"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "b", "status": "QUEUED", "conclusion": None},
            ],
            "a: SUCCESS\nb: PENDING\n",
            3,
        ),
        (
            [
                {"name": "a", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "b", "status": "QUEUED", "conclusion": None},
            ],
            "a: FAILURE\nb: PENDING\n",
            1,
        ),
        (
            [{"name": "optional", "status": "COMPLETED", "conclusion": "SKIPPED"}],
            "optional: SKIPPED\n",
            0,
        ),
        (
            [{"name": "optional", "status": "COMPLETED", "conclusion": "NEUTRAL"}],
            "optional: NEUTRAL\n",
            0,
        ),
        ([{"context": "external CI", "state": "SUCCESS"}], "external CI: SUCCESS\n", 0),
        ([{"context": "external CI", "state": "FAILURE"}], "external CI: FAILURE\n", 1),
        ([{"context": "external CI", "state": "ERROR"}], "external CI: ERROR\n", 1),
        (
            [{"context": "external CI", "state": "EXPECTED"}],
            "external CI: EXPECTED\n",
            1,
        ),
        ([{"context": "external CI", "state": "PENDING"}], "external CI: PENDING\n", 3),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "TIMED_OUT"}],
            "tests: TIMED_OUT\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "CANCELLED"}],
            "tests: CANCELLED\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "ACTION_REQUIRED"}],
            "tests: ACTION_REQUIRED\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}],
            "tests: STARTUP_FAILURE\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "STALE"}],
            "tests: STALE\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "success"}],
            "tests: success\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "MYSTERY"}],
            "tests: MYSTERY\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": ""}],
            "tests: (none)\n",
            1,
        ),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": None}],
            "tests: (none)\n",
            1,
        ),
        (
            [{"name": "tests", "status": "in_progress", "conclusion": ""}],
            "tests: in_progress\n",
            1,
        ),
        (
            [{"name": "tests", "status": "MYSTERY", "conclusion": ""}],
            "tests: MYSTERY\n",
            1,
        ),
        ([{"name": "tests"}], "tests: (none)\n", 1),
        *[
            ([{"name": "tests", "status": status, "conclusion": "FAILURE"}], out, 1)
            for status, out in [
                ("SUCCESS", "tests: SUCCESS\n"),
                ("NEUTRAL", "tests: NEUTRAL\n"),
                ("SKIPPED", "tests: SKIPPED\n"),
            ]
        ],
        *[
            ([{"context": "ci", "state": state, "conclusion": "SUCCESS"}], out, 1)
            for state, out in [
                ("NEUTRAL", "ci: NEUTRAL\n"),
                ("SKIPPED", "ci: SKIPPED\n"),
                ("COMPLETED", "ci: COMPLETED\n"),
            ]
        ],
        (
            [
                {"name": "slow", "status": "QUEUED"},
                {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "slow: PENDING\ntests: SUCCESS\n",
            3,
        ),
        (
            [
                {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "slow", "status": "QUEUED"},
                {"name": "other", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "tests: SUCCESS\nslow: PENDING\nother: SUCCESS\n",
            3,
        ),
        ([{"name": "tests", "status": ["QUEUED"]}], "tests: (none)\n", 1),
        ([{"name": "tests", "status": {"a": 1}}], "tests: (none)\n", 1),
        ([{"context": "ci", "state": ["PENDING"]}], "ci: (none)\n", 1),
        (
            [{"name": "tests", "status": "COMPLETED", "conclusion": "PENDING"}],
            "tests: PENDING\n",
            1,
        ),
        (
            [
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "context": None,
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
            "tests: SUCCESS\n",
            0,
        ),
        (
            [
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "context": "other",
                    "state": "SUCCESS",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
            "tests: FAILURE\n",
            1,
        ),
        (
            [
                {
                    "__typename": "StatusContext",
                    "name": "ignored",
                    "context": "external CI",
                    "state": "SUCCESS",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
            "external CI: SUCCESS\n",
            0,
        ),
        ([{"context": "external CI", "state": "mystery"}], "external CI: mystery\n", 1),
    ],
)
def test_checks_prints_the_head_and_each_job(
    world: World,
    capsys: pytest.CaptureFixture[str],
    jobs: list[dict[str, object]] | None,
    expected: str,
    code: int,
) -> None:
    sha = "a1b2c3d4" * 5
    data = {"headRefOid": sha, "statusCheckRollup": jobs}
    world.fake("gh", stdout=json.dumps(data))
    assert run_main("checks", "45") == code
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (f"{sha}\n{expected}", "")
    (call,) = world.calls("gh")
    assert call["argv"] == [
        "pr",
        "view",
        "45",
        "--json",
        "headRefOid,statusCheckRollup",
    ]
    assert call["stdin"] == ""
    assert call["cwd"] == str(world.root)


@pytest.mark.parametrize("jobs", [[], None])
def test_checks_fails_without_any_check(
    world: World, capsys: pytest.CaptureFixture[str], jobs: list[object] | None
) -> None:
    sha = "a1b2c3d4" * 5
    world.fake("gh", stdout=json.dumps({"headRefOid": sha, "statusCheckRollup": jobs}))
    assert run_main("checks", "45") == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (f"{sha}\n", "no checks on PR 45\n")


@pytest.mark.parametrize(
    "job",
    [
        {"status": "COMPLETED"},
        {"name": ""},
        {"name": 4},
        {"__typename": "StatusContext", "name": "tests", "state": "SUCCESS"},
        {"__typename": "Other", "name": "tests"},
        {"__typename": ["CheckRun"], "name": "tests"},
        {"__typename": {"x": 1}, "name": "tests"},
        {"name": "tests", "context": "other", "state": "SUCCESS"},
    ],
)
def test_checks_names_a_check_without_a_name(
    world: World, job: dict[str, object]
) -> None:
    data = {"headRefOid": "a1b2c3d4" * 5, "statusCheckRollup": [job]}
    world.fake("gh", stdout=json.dumps(data))
    with pytest.raises(
        SystemExit, match=r"^a check of the rollup has no usable name or context"
    ):
        run_main("checks", "45")


@pytest.mark.parametrize("rollup", [7, True, "bad", {"a": 1}])
def test_checks_refuses_a_rollup_that_is_not_a_list(
    world: World, rollup: object
) -> None:
    data = {"headRefOid": "a1b2c3d4" * 5, "statusCheckRollup": rollup}
    world.fake("gh", stdout=json.dumps(data))
    with pytest.raises(SystemExit, match=r"^the checks of PR 45 aren't a list"):
        run_main("checks", "45")


@pytest.mark.parametrize("job", [None, "bad", 7, []])
def test_checks_refuses_an_entry_that_is_not_an_object(
    world: World, job: object
) -> None:
    data = {"headRefOid": "a1b2c3d4" * 5, "statusCheckRollup": [job]}
    world.fake("gh", stdout=json.dumps(data))
    with pytest.raises(SystemExit, match=r"^a check of the rollup isn't an object"):
        run_main("checks", "45")


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (
            {"rc": 1, "stdout": "", "stderr": "no pull requests found for branch 45\n"},
            "no pull requests found for branch 45",
        ),
        ({"rc": 1, "stdout": "{}", "stderr": "not found"}, "not found"),
        ({"rc": 1, "stdout": "not found\n", "stderr": ""}, "not found"),
        ({"rc": 0, "stdout": "not json\n", "stderr": ""}, "not json"),
        ({"rc": 0, "stdout": "[]", "stderr": ""}, "[]"),
    ],
)
def test_checks_stops_when_the_pr_cannot_be_read(
    world: World,
    capsys: pytest.CaptureFixture[str],
    answer: dict[str, object],
    why: str,
) -> None:
    world.fake("gh", cases={"pr view": answer})
    with pytest.raises(
        SystemExit, match=f"^cannot read checks of PR 45: {re.escape(why)}$"
    ):
        run_main("checks", "45")
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


@pytest.mark.parametrize("argv", [(), ("abc",)])
def test_checks_requires_a_pr_number(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("checks", *argv)
    assert info.value.code == 2


READY_VIEW = "pr view 45 --repo github.com/tbhb-dev/example --json author --jq"
READY_ME = "api --hostname github.com user --jq .login"
READY_REPO = "github.com/tbhb-dev/example"


def ready_cases(
    author: str = "agent", me: str = "agent"
) -> dict[str, dict[str, object]]:
    return {
        "repo view": branch_cases()["repo view"],
        READY_VIEW: {"rc": 0, "stdout": f"{author}\n", "stderr": ""},
        READY_ME: {"rc": 0, "stdout": f"{me}\n", "stderr": ""},
    }


def gh_argvs(world: World) -> list[list[str]]:
    calls = world.calls("gh.repo") + world.calls("gh")
    return [cast("list[str]", c["argv"]) for c in calls]


def test_ready_marks_the_pull_request_ready(world: World) -> None:
    world.fake("gh", cases=ready_cases())
    assert run_main("ready", "45") == 0
    assert gh_argvs(world) == [
        ["repo", "view", ORIGIN, "--json", "nameWithOwner,url"],
        [
            "pr",
            "view",
            "45",
            "--repo",
            READY_REPO,
            "--json",
            "author",
            "--jq",
            ".author.login",
        ],
        ["api", "--hostname", "github.com", "user", "--jq", ".login"],
        ["pr", "ready", "45", "--repo", READY_REPO],
    ]
    assert [c["stdin"] for c in world.calls("gh")] == ["", "", ""]


def test_ready_accepts_a_pull_request_the_driver_opened(world: World) -> None:
    world.fake("gh", cases=ready_cases(author="app/tbhb-dev-agent-pull-requests"))
    assert run_main("ready", "45") == 0
    assert gh_argvs(world)[-1] == ["pr", "ready", "45", "--repo", READY_REPO]


def test_refusal_reason_decodes_invalid_bytes_and_joins_streams() -> None:
    assert pr.refusal_reason(b"bad \xff byte\n", b"and\r\nmore") == (
        "bad \ufffd byte and more"
    )


@pytest.mark.parametrize(
    ("author", "me"),
    [
        ("human", "agent"),
        ("app/some-bot", "agent"),
        ("app/", "agent"),
        ("null", "agent"),
        ("Agent", "agent"),
        (" agent", "agent"),
        ("agent ", "agent"),
        ("", ""),
        (" ", " "),
        ("agent\r", "agent"),
        ("agent", "agent\r"),
    ],
)
def test_ready_refuses_a_login_that_is_not_the_agents(
    world: World, author: str, me: str
) -> None:
    cases = ready_cases(author=author, me=me)
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit, match=r"isn't this agent's account$"):
        run_main("ready", "45")
    assert not any(a[:2] == ["pr", "ready"] for a in gh_argvs(world))


@pytest.mark.parametrize("failing", [READY_VIEW, READY_ME])
def test_ready_refuses_when_a_login_is_unreadable(world: World, failing: str) -> None:
    cases = ready_cases()
    cases[failing] = {"rc": 1, "stdout": "agent\n", "stderr": "bad\n token"}
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit, match=r"isn't this agent's account: bad token$"):
        run_main("ready", "45")
    assert not any(a[:2] == ["pr", "ready"] for a in gh_argvs(world))


@pytest.mark.parametrize(
    ("stderr", "shown"),
    [
        ("see https://example.test/x?token=abc now", "see <redacted> now"),
        (
            "bad " + "gh" + "p_" + "0123456789abcdefABCDEF" + " token",
            "bad <redacted> token",
        ),
        ("a" * 19, "a" * 19),
        ("z" * 20, "<redacted>"),
        ("w " * 150, "w " * 100),
    ],
)
def test_ready_refusal_redacts_and_bounds_gh_stderr(
    world: World, stderr: str, shown: str
) -> None:
    cases = ready_cases()
    cases[READY_VIEW] = {"rc": 1, "stdout": "agent\n", "stderr": stderr}
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit) as info:
        run_main("ready", "45")
    message = str(info.value)
    assert message.endswith(f"account: {shown}")


def test_ready_acts_on_origin_even_when_gh_defaults_elsewhere(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The author read and the ready call name origin, not GH_REPO or GH_HOST."""
    monkeypatch.setenv("GH_REPO", "other.example/someone/else")
    monkeypatch.setenv("GH_HOST", "other.example")
    world.fake("gh", cases=ready_cases())
    assert run_main("ready", "45") == 0
    pinned = [a for a in gh_argvs(world) if a[:2] in (["pr", "view"], ["pr", "ready"])]
    assert len(pinned) == 2
    assert all(a[a.index("--repo") + 1] == READY_REPO for a in pinned)


@pytest.mark.parametrize(
    "argv",
    [
        ["delete-branch"],
        ["delete-branch", "abc"],
    ],
)
def test_delete_branch_requires_a_numeric_pr_number(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        run_main(*argv)
    assert exc.value.code == 2


def branch_cases(**changes: object) -> dict[str, dict[str, object]]:
    data: dict[str, object] = {
        "state": "CLOSED",
        "headRefName": "agent/topic",
        "isCrossRepository": False,
    }
    data.update(changes)
    return {
        "repo view": {
            "rc": 0,
            "stdout": json.dumps(
                {
                    "nameWithOwner": "tbhb-dev/example",
                    "url": "https://github.com/tbhb-dev/example",
                }
            ),
            "stderr": "",
        },
        "pr view": {"rc": 0, "stdout": json.dumps(data), "stderr": ""},
        BRANCH_LOOKUP: {
            "rc": 0,
            "stdout": json.dumps({"name": "agent/topic", "protected": False}),
            "stderr": "",
        },
    }


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_delete_branch_deletes_only_the_origin_pr_head(
    world: World, state: str
) -> None:
    world.fake(
        "gh", cases=branch_cases(state=state), stdout="deleted\n", stderr="warn\n"
    )
    assert run_main("delete-branch", "45") == 0
    calls = world.calls("gh.repo") + world.calls("gh")
    assert [call["argv"] for call in calls] == [
        ["repo", "view", ORIGIN, "--json", "nameWithOwner,url"],
        [
            "pr",
            "view",
            "45",
            "--repo",
            "github.com/tbhb-dev/example",
            "--json",
            "state,headRefName,isCrossRepository",
        ],
        [
            "api",
            "--hostname",
            "github.com",
            "repos/tbhb-dev/example/branches/agent%2Ftopic",
        ],
        [
            *["api", "--hostname", "github.com"],
            *["--method", "DELETE"],
            "repos/tbhb-dev/example/git/refs/heads/agent%2Ftopic",
        ],
    ]
    assert all(call["stdin"] == "" for call in calls)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"state": "OPEN"}, "PR 45 is not closed or merged"),
        ({"state": None}, "PR 45 is not closed or merged"),
        ({"isCrossRepository": True}, "PR 45 head is not on origin"),
        ({"isCrossRepository": None}, "PR 45 head is not on origin"),
        ({"headRefName": "main"}, "refusing to delete main"),
        ({"headRefName": ""}, "cannot read the head branch of PR 45"),
        ({"headRefName": None}, "cannot read the head branch of PR 45"),
        ({"headRefName": "topic/../main"}, "invalid head branch of PR 45"),
    ],
)
def test_delete_branch_refuses_unsafe_prs(
    world: World, changes: dict[str, object], message: str
) -> None:
    world.fake("gh", cases=branch_cases(**changes))
    with pytest.raises(SystemExit, match=f"^{message}$"):
        run_main("delete-branch", "45")
    assert len(world.calls("gh")) == 1


@pytest.mark.parametrize("protected", [True, None, "false", 0])
def test_delete_branch_refuses_protected_or_unknown_branches(
    world: World, protected: object
) -> None:
    cases = branch_cases()
    cases[BRANCH_LOOKUP]["stdout"] = json.dumps(
        {"name": "agent/topic", "protected": protected}
    )
    world.fake("gh", cases=cases)
    with pytest.raises(
        SystemExit, match=r"^branch agent/topic is protected or unreadable$"
    ):
        run_main("delete-branch", "45")
    assert len(world.calls("gh")) == 2


@pytest.mark.parametrize(
    "lookup",
    ["repo view", "pr view", BRANCH_LOOKUP],
)
@pytest.mark.parametrize("output", ["not JSON", "[]", "{}"])
@pytest.mark.parametrize("rc", [0, 3])
def test_delete_branch_refuses_failed_or_malformed_lookups(
    world: World, lookup: str, output: str, rc: int
) -> None:
    cases = branch_cases()
    cases[lookup] = {"rc": rc, "stdout": output, "stderr": "boom\n"}
    world.fake("gh", cases=cases)
    with pytest.raises(SystemExit):
        run_main("delete-branch", "45")
    assert all(
        "DELETE" not in cast("list[str]", call["argv"]) for call in world.calls("gh")
    )


def test_delete_branch_requires_origin(world: World) -> None:
    world.git("remote", "remove", "origin")
    with pytest.raises(SystemExit, match=r"^cannot read origin$"):
        run_main("delete-branch", "45")
    assert world.calls("gh") == []
    assert world.calls("gh.repo") == []


@pytest.mark.parametrize(
    "url",
    [None, "https://example.test/tbhb-dev/example", "https://github.com/other/repo"],
)
def test_delete_branch_requires_a_matching_github_origin(
    world: World, url: object
) -> None:
    cases = branch_cases()
    cases["repo view"]["stdout"] = json.dumps(
        {"nameWithOwner": "tbhb-dev/example", "url": url}
    )
    world.fake("gh", cases=cases)
    with pytest.raises(
        SystemExit, match=r"^origin must name a repository on github.com$"
    ):
        run_main("delete-branch", "45")
    assert world.calls("gh") == []


def test_delete_branch_refuses_mismatched_branch_metadata(world: World) -> None:
    cases = branch_cases()
    cases[BRANCH_LOOKUP]["stdout"] = json.dumps({"name": "main", "protected": False})
    world.fake("gh", cases=cases)
    with pytest.raises(
        SystemExit, match=r"^branch agent/topic is protected or unreadable$"
    ):
        run_main("delete-branch", "45")
    assert len(world.calls("gh")) == 2


def test_delete_branch_passes_gh_output_and_exit_code_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", cases=branch_cases(), rc=3, stdout="out\n", stderr="err\n")
    assert run_main("delete-branch", "45") == 3
    assert capsys.readouterr() == ("out\n", "err\n")


def test_ready_marks_each_pull_request_in_order(world: World) -> None:
    cases = ready_cases()
    cases[READY_VIEW.replace("view 45", "view 46")] = cases[READY_VIEW]
    world.fake("gh", cases=cases)
    assert run_main("ready", "45", "46") == 0
    marked = [a[2] for a in gh_argvs(world) if a[:2] == ["pr", "ready"]]
    assert marked == ["45", "46"]


def test_ready_stops_at_the_first_failure(world: World) -> None:
    cases = ready_cases()
    cases["pr ready"] = {"rc": 3, "stdout": "", "stderr": ""}
    world.fake("gh", cases=cases)
    assert run_main("ready", "45", "46") == 3
    assert [a[2] for a in gh_argvs(world) if a[:2] == ["pr", "ready"]] == ["45"]
    assert not any(a[:3] == ["pr", "view", "46"] for a in gh_argvs(world))


def test_ready_passes_gh_output_and_exit_code_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = ready_cases()
    cases["pr ready"] = {"rc": 3, "stdout": "out\n", "stderr": "err\n"}
    world.fake("gh", cases=cases)
    assert run_main("ready", "45") == 3
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("out\n", "err\n")


def test_ready_requires_a_pr_number() -> None:
    with pytest.raises(SystemExit) as info:
        run_main("ready")
    assert info.value.code == 2


def test_ready_rejects_a_pr_that_is_not_a_number() -> None:
    with pytest.raises(SystemExit) as info:
        run_main("ready", "abc")
    assert info.value.code == 2


REBASE_LINE = (
    "- [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box"
)
REBASE_BODY = f"""## Renovate updates

This PR updates `example` from 1.0.0 to 1.1.0.

### Configuration

- [ ] Approve this update
{REBASE_LINE}

Generated by Renovate • see the changelog.
"""
REBASE_LOGIN = "tbhb-dev-renovate[bot]"
TICKED_REBASE_LINE = REBASE_LINE.replace("- [ ]", "- [x]")
TICKED_REBASE_BODY = REBASE_BODY.replace(REBASE_LINE, TICKED_REBASE_LINE)


def rebase_view(**fields: object) -> subprocess.CompletedProcess[bytes]:
    data = {"author": {"login": REBASE_LOGIN}, "state": "OPEN", "body": REBASE_BODY}
    return subprocess.CompletedProcess([], 0, json.dumps(data | fields).encode(), b"")


def fake_rebase_gh(
    monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess[bytes]
) -> tuple[Mock, Mock]:
    read = Mock(side_effect=responses)
    write = Mock(return_value=(0, ""))
    monkeypatch.setattr(pr, "origin_repository", Mock(return_value="tbhb-dev/example"))
    monkeypatch.setattr(
        pr, "find_tool", Mock(side_effect={"gh": "/bin/gh"}.__getitem__)
    )
    monkeypatch.setattr(pr, "run_bytes", read)
    monkeypatch.setattr(pr, "invoke_gh", write)
    return read, write


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing", [True, False])
def test_rebase_ticks_only_the_renovate_checkbox(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ending: str,
    trailing: bool,
) -> None:
    body = (REBASE_BODY if trailing else REBASE_BODY.rstrip("\n")).replace("\n", ending)
    expected = body.replace(REBASE_LINE, TICKED_REBASE_LINE)
    read, write = fake_rebase_gh(monkeypatch, rebase_view(body=body))
    assert run_main("rebase", "8") == 0
    target = ["8", "--repo", "github.com/tbhb-dev/example"]
    read.assert_called_once_with(
        ["/bin/gh", "pr", "view", *target, "--json", "author,state,body"]
    )
    write.assert_called_once_with(
        ["gh", "pr", "edit", *target, "--body-file", "-"], expected, quiet=True
    )
    actual = cast("str", write.call_args.args[1]).encode()
    assert len(actual) == len(body.encode())
    assert sum(a != b for a, b in zip(actual, body.encode(), strict=True)) == 1
    assert capsys.readouterr() == ("PR 8: ticked the Renovate rebase checkbox\n", "")


def test_rebase_leaves_other_checkboxes_on_the_marker_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = REBASE_LINE + " (from `- [ ]`)"
    _, write = fake_rebase_gh(monkeypatch, rebase_view(body=body))
    assert run_main("rebase", "8") == 0
    assert write.call_args.args[1] == body.replace("- [ ]", "- [x]", 1)


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        (
            {"author": {"login": "app/tbhb-dev-agent-pull-requests"}},
            f"author is not {REBASE_LOGIN}",
        ),
        ({"author": {"login": REBASE_LOGIN.upper()}}, f"author is not {REBASE_LOGIN}"),
        ({"author": None}, f"author is not {REBASE_LOGIN}"),
        ({"state": "CLOSED"}, "pull request is not open"),
        ({"body": None}, "cannot read pull request body"),
        ({"body": "- [ ] unrelated checkbox\n"}, "no Renovate rebase checkbox line"),
        (
            {"body": f"{REBASE_LINE}\n{REBASE_LINE}"},
            "multiple Renovate rebase checkbox lines",
        ),
        ({"body": REBASE_LINE[6:]}, "malformed Renovate rebase checkbox line"),
    ],
)
def test_rebase_refuses_an_ineligible_pull_request(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, object], why: str
) -> None:
    read, write = fake_rebase_gh(monkeypatch, rebase_view(**fields))
    with pytest.raises(SystemExit, match=f"^PR 8: {re.escape(why)}$"):
        run_main("rebase", "8", "9")
    assert read.call_count == 1
    write.assert_not_called()


def test_rebase_refuses_when_pr_view_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    response = subprocess.CompletedProcess([], 1, rebase_view().stdout, b"failed")
    _, write = fake_rebase_gh(monkeypatch, response)
    with pytest.raises(SystemExit, match=r"^PR 8: cannot read pull request on origin$"):
        run_main("rebase", "8")
    write.assert_not_called()


@pytest.mark.parametrize(
    ("code", "login", "accepted"),
    [(0, REBASE_LOGIN, True), (1, REBASE_LOGIN, False), (0, "other[bot]", False)],
)
def test_rebase_verifies_the_app_login(
    monkeypatch: pytest.MonkeyPatch, code: int, login: str, accepted: bool
) -> None:
    view = rebase_view(author={"login": "app/tbhb-dev-renovate"})
    canonical = subprocess.CompletedProcess([], code, f"{login}\n".encode(), b"")
    read, write = fake_rebase_gh(monkeypatch, view, canonical)
    if accepted:
        assert run_main("rebase", "8") == 0
        assert write.call_count == 1
    else:
        with pytest.raises(
            SystemExit, match=f"^PR 8: author is not {re.escape(REBASE_LOGIN)}$"
        ):
            run_main("rebase", "8")
        write.assert_not_called()
    assert read.call_count == 2
    assert read.call_args.args == (
        [
            "/bin/gh",
            "api",
            "--hostname",
            "github.com",
            "repos/tbhb-dev/example/pulls/8",
            "--jq",
            ".user.login",
        ],
    )


def test_rebase_already_ticked_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ticked = rebase_view(body=TICKED_REBASE_BODY)
    _, write = fake_rebase_gh(monkeypatch, ticked, ticked)
    assert run_main("rebase", "8", "9") == 0
    write.assert_not_called()
    assert capsys.readouterr() == (
        (
            "PR 8: the Renovate rebase checkbox is already ticked\n"
            "PR 9: the Renovate rebase checkbox is already ticked\n"
        ),
        "",
    )


def test_rebase_stops_at_the_first_failed_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    read, _ = fake_rebase_gh(monkeypatch, rebase_view(), rebase_view())
    write = Mock(side_effect=[(0, ""), (3, "")])
    monkeypatch.setattr(pr, "invoke_gh", write)
    assert run_main("rebase", "8", "9", "10") == 3
    assert [call.args[0][3] for call in write.call_args_list] == ["8", "9"]
    assert [call.args[0][3] for call in read.call_args_list] == ["8", "9"]
    assert capsys.readouterr() == (
        "PR 8: ticked the Renovate rebase checkbox\n",
        "PR 9: could not tick the Renovate rebase checkbox\n",
    )


@pytest.mark.parametrize(
    "rewritten",
    [
        REBASE_BODY,
        TICKED_REBASE_BODY + "\n",
        TICKED_REBASE_BODY.replace("by Renovate", "by upstream"),
        TICKED_REBASE_BODY.replace(
            f"{TICKED_REBASE_LINE}\n\n", f"{TICKED_REBASE_LINE}\n\r"
        ),
    ],
)
def test_rebase_verifies_the_whole_body_before_writing(
    monkeypatch: pytest.MonkeyPatch, rewritten: str
) -> None:
    _, write = fake_rebase_gh(monkeypatch, rebase_view())
    monkeypatch.setattr(pr, "tick_rebase_checkbox", Mock(return_value=rewritten))
    with pytest.raises(
        SystemExit, match=r"^PR 8: cannot verify a checkbox-only change$"
    ):
        run_main("rebase", "8")
    write.assert_not_called()


def test_rebase_prints_only_its_status(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    body = REBASE_BODY.rstrip("\n").replace("\n", "\r\n")
    cases = ready_cases()
    cases["pr view 8"] = {
        "rc": 0,
        "stdout": rebase_view(body=body).stdout.decode(),
        "stderr": "",
    }
    cases["pr edit 8"] = {
        "rc": 0,
        "stdout": pr_page("tbhb-dev/example", 8) + "\n",
        "stderr": "warning\n",
    }
    world.fake("gh", cases=cases)
    assert run_main("rebase", "8") == 0
    assert capsys.readouterr() == ("PR 8: ticked the Renovate rebase checkbox\n", "")
    assert world.calls("gh")[-1]["stdin"] == body.replace(
        REBASE_LINE, TICKED_REBASE_LINE
    )


@pytest.mark.parametrize("args", [(), ("abc",)])
def test_rebase_requires_numeric_pr_numbers(args: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("rebase", *args)
    assert info.value.code == 2


def test_pr_help_lists_rebase_and_close(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run_main("--help")
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "ready,rebase,rerun" in out
    assert "close N --reason TEXT: close a pull request" in out


def test_backfill_needs_an_owner() -> None:
    with pytest.raises(SystemExit) as info:
        run_main("backfill-opened")
    assert info.value.code == 2


def test_an_opened_by_line_does_not_hide_later_trailers() -> None:
    assert structure(BASE + "\nOpened-by: x\nGenerated-by: LLM x\n") == [
        (15, pr.OPENED_MESSAGE),
        (16, "the script writes Generated-by and Assisted-by; remove this line"),
    ]


def test_edit_stops_when_gh_fails_with_a_body(world: World) -> None:
    answer: dict[str, object] = {"rc": 1, "stdout": '{"body": ""}', "stderr": ""}
    world.fake("gh", cases={"pr view": answer})
    write_draft(world)
    message = 'cannot read the body of PR 7 to keep its Opened-by: {"body": ""}'
    with pytest.raises(SystemExit, match=f"^{re.escape(message)}$"):
        run_main("edit", str(pr.DRAFT), "--pr", "7")


def test_backfill_stops_when_gh_fails_with_a_list(world: World) -> None:
    world.fake("gh", cases={"repo list": gh_list([], rc=1)})
    with pytest.raises(SystemExit, match=r"^gh repo list failed: \[\]$"):
        run_main("backfill-opened", "tbhb-dev")


def test_every_invalid_trailer_is_joined_by_semicolons(world: World) -> None:
    world.git("switch", "-q", "-c", "topic")
    world.commit("one\n\nGenerated-by: LLM model=a:m\nAssisted-by: LLM model=b:m")
    with pytest.raises(SystemExit) as info:
        pr.description(checked_for(), {}, pr.branch_trailers("main"))
    assert str(info.value) == (
        "invalid LLM trailer: LLM model=a:m: harness required; "
        "LLM model=b:m: harness required"
    )


def test_a_dry_run_reports_success_and_no_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert pr.invoke_gh(["gh", "pr"], "b\n", dry_run=True) == (0, "")
    assert capsys.readouterr().out == "gh pr\n\nb\n"


def test_help_uses_the_program_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        pr.main(["--help"], {}, prog="tbhbdev alpha pr")
    assert capsys.readouterr().out.startswith("usage: tbhbdev alpha pr ")


def test_help_lists_close(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        pr.main(["--help"], {}, prog="tbhbdev alpha pr")
    assert "close N --reason TEXT: close a pull request" in capsys.readouterr().out


@pytest.mark.parametrize("make", ["file", "bytes"])
def test_create_warns_when_the_ledger_cant_be_written(
    world: World,
    agent: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    make: str,
) -> None:
    store = ledgers(world)
    store.mkdir(parents=True)
    path = store / f"{agent_identity.hx(ROOT)}.json"
    if make == "file":
        # A directory where the ledger file goes: the write fails with OSError.
        path.mkdir()
        why = f"cannot read {path}: Is a directory"
    else:
        path.write_bytes(b'{"prs": ["\xff"]}')
        why = f"{path} is not UTF-8"
    write_draft(world)
    assert run_main("create", env=agent) == 0
    out, err = capsys.readouterr()
    assert out == "https://github.com/tbhb-dev/tools/pull/1\n"
    assert err == f"warning: the PR is open, but the ledger was not updated: {why}\n"


def test_create_warns_when_the_write_fails(
    world: World,
    agent: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(opened, "write_private_json", full)
    write_draft(world)
    assert run_main("create", env=agent) == 0
    assert capsys.readouterr().err == (
        "warning: the PR is open, but the ledger was not updated: "
        "[Errno 28] No space left on device\n"
    )


def test_an_opener_without_a_model_gets_no_line_and_no_ledger(
    world: World, agent: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    child = agent_identity.sha256_64("agent-raw")
    agent_identity.write_user_record(child, {"root": ROOT, "model": "claude-x"})
    seed_session(world, None)
    write_draft(world)
    assert run_main("create", env={**agent, "ASCM_AGENT_ID": child}) == 0
    _, call = world.calls("gh")
    assert "Opened-by" not in str(call["stdin"])
    assert not ledgers(world).exists()
    value = f"LLM harness=claude-code:2.1.0;session={ROOT};surface=cli"
    assert capsys.readouterr().err == (
        f"warning: no Opened-by line or ledger entry: {value}: model required\n"
    )


def test_backfill_warns_at_each_limit(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr, "REPO_LIMIT", 2)
    monkeypatch.setattr(pr, "PR_LIMIT", 4)
    world.fake("gh", cases=BACKFILL_CASES)
    assert run_main("backfill-opened", "tbhb-dev") == 0
    assert capsys.readouterr().err == (
        "warning: gh repo list tbhb-dev stopped at its limit of 2; "
        "some may be missing\n"
        "warning: gh pr list -R tbhb-dev/a stopped at its limit of 4; "
        "some may be missing\n"
    )
    argvs = [cast("list[str]", call["argv"]) for call in world.calls("gh")]
    assert [argv[argv.index("--limit") + 1] for argv in argvs] == ["2", "4", "4"]


def test_backfill_is_quiet_below_the_limits(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", cases=BACKFILL_CASES)
    assert run_main("backfill-opened", "tbhb-dev") == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("dry_run", [False, True])
def test_create_refuses_an_open_pr(
    world: World, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    world.fake(
        "gh",
        cases={
            "pr view --json number,state,url": {
                "rc": 0,
                "stdout": json.dumps(
                    {
                        "number": 17,
                        "state": "OPEN",
                        "url": "https://github.com/tbhb-dev/example/pull/17",
                    }
                ),
                "stderr": "",
            }
        },
    )
    write_draft(world)
    assert run_main("create", *(["--dry-run"] if dry_run else [])) == 1
    assert capsys.readouterr() == (
        "",
        (
            "error: PR 17 (https://github.com/tbhb-dev/example/pull/17) "
            "is already open; "
            "run `tbhbdev alpha pr edit DRAFT --pr 17` to apply the draft\n"
        ),
    )
    assert [call["argv"] for call in world.calls("gh")] == [
        ["pr", "view", "--json", "number,state,url"]
    ]


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_create_after_a_finished_pr(world: World, state: str) -> None:
    world.fake(
        "gh",
        stdout="https://github.com/tbhb-dev/example/pull/18\n",
        cases={
            "pr view --json number,state,url": {
                "rc": 0,
                "stdout": json.dumps({"number": 17, "state": state, "url": "old"}),
                "stderr": "",
            }
        },
    )
    write_draft(world)
    assert run_main("create") == 0
    view, create = world.calls("gh")
    assert view["stdin"] == ""
    assert cast("list[str]", create["argv"])[:2] == ["pr", "create"]


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr"),
    [
        (1, "", "authentication failed"),
        (0, "not JSON", ""),
        (0, "[]", ""),
        (0, "{}", ""),
        *[
            (
                0,
                json.dumps({"state": "OPEN", "number": 17, "url": "old", key: value}),
                "",
            )
            for key in ("state", "number", "url")
            for value in (None, [1], {"value": 1}, True, 1.5)
        ],
        *[
            (
                0,
                json.dumps(
                    {
                        key: value
                        for key, value in {
                            "state": "OPEN",
                            "number": 17,
                            "url": "old",
                        }.items()
                        if key != missing
                    }
                ),
                "",
            )
            for missing in ("state", "number", "url")
        ],
        *[
            (
                0,
                json.dumps({"state": "OPEN", "number": 17, "url": "old", key: value}),
                "",
            )
            for key, value in (("state", 17), ("number", "17"), ("url", 17))
        ],
        (1, "", "authentication failed\n\n"),
        (1, "", "authentication failed \n"),
        (1, "", "authentication error X\n"),
        (0, "", "no pull requests found"),
    ],
)
def test_create_stops_when_the_lookup_fails(
    world: World, capsys: pytest.CaptureFixture[str], rc: int, stdout: str, stderr: str
) -> None:
    world.fake(
        "gh",
        cases={
            "pr view --json number,state,url": {
                "rc": rc,
                "stdout": stdout,
                "stderr": stderr,
            }
        },
    )
    write_draft(world)
    assert run_main("create") == 1
    assert capsys.readouterr() == (
        "",
        f"cannot check for an open PR: {(stderr or stdout).rstrip(chr(10))}\n",
    )
    assert len(world.calls("gh")) == 1


@pytest.mark.parametrize(
    "result", [(rc, ending) for rc in (0, 1) for ending in ("", "\n", "\n\n")]
)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("url", ["", "https://github.com/tbhb-dev/example/pull/17"])
def test_create_rejects_already_exists(
    world: World,
    capsys: pytest.CaptureFixture[str],
    result: tuple[int, str],
    stream: str,
    url: str,
) -> None:
    rc, ending = result
    output = f"a pull request Already Exists {url}{ending}"
    answer: dict[str, object] = {"rc": rc, "stdout": "", "stderr": "", stream: output}
    world.fake("gh", cases={"pr create": answer})
    write_draft(world)
    assert run_main("create") == 1
    number = "17" if url else "<n>"
    assert capsys.readouterr() == (
        "",
        output.rstrip("\n")
        + "\n"
        + (
            f"gh pr create failed; run `tbhbdev alpha pr edit DRAFT --pr {number}` "
            "to apply the draft\n"
        ),
    )
    assert not ledgers(world).exists()


def test_create_passes_success_stderr_through(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fake("gh", stdout="created\n", stderr="warning\n")
    write_draft(world)
    assert run_main("create") == 0
    assert capsys.readouterr() == ("created\n", "warning\n")


@pytest.mark.parametrize("ending", ["", "\n", "\n\n"])
def test_create_joins_error_streams(
    world: World, capsys: pytest.CaptureFixture[str], ending: str
) -> None:
    world.fake(
        "gh",
        cases={
            "pr create": {
                "rc": 1,
                "stdout": "request failed" + ending,
                "stderr": "connection lost" + ending,
            }
        },
    )
    write_draft(world)
    assert run_main("create") == 1
    assert capsys.readouterr() == ("", "request failed\nconnection lost\n")


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("request failed\n", "\n\n", "request failed\n"),
        ("\n\n", "connection lost\n", "connection lost\n"),
        (
            "request failed \n",
            "connection lost \n",
            "request failed \nconnection lost \n",
        ),
        ("request X\n", "connection X\n", "request X\nconnection X\n"),
        ("", "", "\n"),
    ],
)
def test_create_normalizes_empty_error_streams(
    world: World,
    capsys: pytest.CaptureFixture[str],
    stdout: str,
    stderr: str,
    expected: str,
) -> None:
    world.fake("gh", cases={"pr create": {"rc": 1, "stdout": stdout, "stderr": stderr}})
    write_draft(world)
    assert run_main("create") == 1
    assert capsys.readouterr() == ("", expected)


def test_origin_repository_reads_the_push_url(world: World) -> None:
    world.git("remote", "set-url", "origin", str(world.root))
    world.git("config", "remote.origin.pushurl", ORIGIN)
    world.fake("gh", cases=branch_cases())
    assert pr.origin_repository() == "tbhb-dev/example"
    assert gh_argvs(world) == [["repo", "view", ORIGIN, "--json", "nameWithOwner,url"]]


def test_origin_repository_without_a_push_url_reads_origin(world: World) -> None:
    world.fake("gh", cases=branch_cases())
    assert pr.origin_repository() == "tbhb-dev/example"
    assert gh_argvs(world) == [["repo", "view", ORIGIN, "--json", "nameWithOwner,url"]]
