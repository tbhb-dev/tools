"""Briefs carry fixed safeguards and reject normalized forbidden instructions."""

import re
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha, cli
from tbhbdev.alpha import brief

if TYPE_CHECKING:
    from pathlib import Path

EXPECTED = (
    """# {TASK} (tbhb-dev/tools, demo)

{step}

## Task

{TASK_DETAILS}

## Gates

{GATES}

## Commits

{commits}

{push}

## Hard rules

{rules}

## Final report

{FINAL_REPORT}
""".replace(
        "{step}",
        brief.STEP_BLOCK.format(
            repo="tbhb-dev/tools",
            path=".worktrees/demo",
            branch="agent/demo",
            base="origin/main",
            hook_rule=brief.HOOK_RULE,
        ),
    )
    .replace("{commits}", brief.COMMITS_BLOCK)
    .replace("{push}", brief.NO_PUSH_BLOCK)
    .replace("{rules}", brief.HARD_RULES_BLOCK)
)
FILLED = re.sub(r"\{[^{}]+\}", "Completed", EXPECTED)
PROG = "example brief"
SCAN_COMMAND = "scan-outgoing origin/main"
SCAN_LINE = f"Outgoing scan command: `{SCAN_COMMAND}`"
PUSH_SECTION = f"## Push\n\n{brief.PUSH_BLOCK}\n{SCAN_LINE}"
MERGE_CONFLICT = (
    "hook conflict: merge commits lack the required Generated-by trailer. "
    "Use git merge --no-commit, then a plain git commit"
)
OMISSION_CONFLICT = "hook conflict: let the hooks stamp the identity trailers"
MANUAL_CONFLICT = (
    "hook conflict: leave hand-written identity trailers out of the commit message"
)
BYPASS_CONFLICT = "hook conflict: hook bypass is forbidden"
PUSH_CONFLICT = "hook conflict: push to main or master is forbidden"
INSTALL_CONFLICT = "hook conflict: hook install is forbidden"
BRANCH_WARNING = "use the launcher branch agent/sample-run instead of "


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            (
                "1. Run `git fetch origin main` then "
                '`git merge --no-ff -m "chore: merge main into the branch" '
                "origin/main`."
            ),
            MERGE_CONFLICT,
        ),
        (
            "Run `git fetch origin main` then `git merge --no-ff origin/main`.",
            MERGE_CONFLICT,
        ),
        ("git merge origin/main; git merge --no-commit origin/main", MERGE_CONFLICT),
        ("git merge origin/main\nUse --no-commit if needed.", MERGE_CONFLICT),
        (
            (
                "Commit with plain git commit; no Signed-off-by, "
                "no Generated-by or Co-authored-by trailers."
            ),
            OMISSION_CONFLICT,
        ),
        ("Commit without an Assisted-by trailer.", OMISSION_CONFLICT),
        ("Omit the Generated-by line.", OMISSION_CONFLICT),
        ("Strip Generated-by.", OMISSION_CONFLICT),
        ('git commit -m "feat: x" -m "Generated-by: LLM model=sol"', MANUAL_CONFLICT),
        ('git commit --message="Assisted-by: example"', MANUAL_CONFLICT),
        ("git commit -F 'Generated-by: example'", MANUAL_CONFLICT),
        ("git commit --file 'Assisted-by: example'", MANUAL_CONFLICT),
        ('git commit --no-verify -m "x"', BYPASS_CONFLICT),
        ("git commit -n", BYPASS_CONFLICT),
        ("git commit -n\ngit status", BYPASS_CONFLICT),
        ("git push --no-verify", BYPASS_CONFLICT),
        ("Avoid skipping hooks. Use --no-verify.", BYPASS_CONFLICT),
        ("git push origin main", PUSH_CONFLICT),
        ("git push origin main\ngit status", PUSH_CONFLICT),
        ("git push origin HEAD:main", PUSH_CONFLICT),
        ("git push master", PUSH_CONFLICT),
        ("git push origin HEAD:master", PUSH_CONFLICT),
        ("git push origin refs/heads/main", PUSH_CONFLICT),
        ("git push origin refs/heads/master", PUSH_CONFLICT),
        ("Run prek install first.", INSTALL_CONFLICT),
        ("Run pre-commit install first.", INSTALL_CONFLICT),
    ],
)
def test_check_hooks_refuses_conflict(text: str, problem: str) -> None:
    assert brief.check_hooks(text) == [problem]


@pytest.mark.parametrize(
    ("text", "branch"),
    [
        (
            (
                "Create branch feat/graph-baseline from origin/main "
                "and commit the work there."
            ),
            "feat/graph-baseline",
        ),
        ("git switch -c feat/graph-baseline", "feat/graph-baseline"),
        ("git checkout -b fix/other origin/main", "fix/other"),
        ("git checkout -B fix/other", "fix/other"),
        ("git checkout fix/other", "fix/other"),
        ("git switch -C fix/other", "fix/other"),
        ("git switch fix/other", "fix/other"),
        ("git branch fix/other", "fix/other"),
        ("git worktree add .worktrees/other -b fix/other", "fix/other"),
        ("Commit on branch `fix/other`.", "fix/other"),
        ("Work on branch fix/other.", "fix/other"),
        ("git branch agent/other", "agent/other"),
        ("git switch -c agent/Sample-run", "agent/Sample-run"),
    ],
)
def test_foreign_branch_warns_without_a_hook_conflict(text: str, branch: str) -> None:
    assert brief.check_hooks(text) == []
    assert brief.branch_warnings(text, run="sample-run") == [BRANCH_WARNING + branch]


@pytest.mark.parametrize(
    "text",
    [
        "Never use --no-verify.",
        (
            "No hook skips (no SKIP=, PREK_SKIP=, --no-verify, hooksPath override); "
            "a hook failure means stop and report."
        ),
        (
            "Never run `prek install`, `pre-commit install`, `tbhbdev alpha install`, "
            "or any other hook installer."
        ),
        "(never bare `prek install`)",
        "Never push to main. Push with `git push origin HEAD:docs/some-branch`.",
        (
            "git merge --no-ff --no-commit origin/main then finish with a plain "
            '`git commit -m "chore(o11y): merge main"` (no -F, no --no-verify).'
        ),
        "git merge-base --is-ancestor 969da64 HEAD",
        "git merge --ff-only origin/main",
        "git merge --squash origin/main",
        "git merge --abort",
        "git merge --abort\ngit status",
        "git merge --continue",
        "git merge --quit",
        "Avoid `git merge --no-ff origin/main`.",
        (
            "No Signed-off-by, no Co-authored-by. Author and committer must both read "
            "the agent identity; the hooks stamp the Generated-by trailer."
        ),
        (
            "Commit once (Conventional Commits, no Signed-off-by or Co-authored-by; "
            "the Generated-by trailer must name harness and model)."
        ),
        "Never remove the Generated-by trailer.",
        'Do not use `git commit -m "Generated-by: example"`.',
        'git commit -m "feat: x"',
        "git commit -F msg.txt",
        "Do not push to main; the driver pushes your agent/sample-run branch.",
        "Do not run `git push origin HEAD:main`.",
        "Never create or switch branches.",
        "Do not create branch fix/other.",
        "Don't run `git switch -c fix/other`.",
        "git switch -c agent/sample-run",
        "git worktree add .worktrees/sample-run -b agent/sample-run origin/main",
        "git checkout origin/main -- README.md",
        "git branch --list",
        "This fixes the main branch build.",
        "git push origin HEAD:docs/main",
        "git push origin mainland",
        "git push origin topic; echo main",
        "git push origin topic\nmain is the default branch.",
        brief.COMMITS_BLOCK,
        brief.PUSH_BLOCK,
        brief.HARD_RULES_BLOCK,
    ],
)
def test_check_hooks_accepts_clean_text(text: str) -> None:
    assert brief.check_hooks(text) == []
    assert brief.branch_warnings(text, run="sample-run") == []


@pytest.mark.parametrize("scan_command", [None, SCAN_COMMAND])
def test_check_hooks_accepts_filled_skeleton(scan_command: str | None) -> None:
    text = brief.render("sample-run", "other/project", scan_command=scan_command)
    filled = re.sub(r"\{[^{}]+\}", "Completed", text)
    assert brief.check_hooks(filled) == []
    assert brief.branch_warnings(filled, run="sample-run") == []
    assert brief.branch_warnings(filled) == []


def test_check_warns_about_foreign_branch_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "brief.md"
    file.write_text(FILLED + "\ngit branch fix/other\n")
    assert brief.main(["check", str(file)], PROG) == 0
    assert capsys.readouterr() == (
        (
            f"warning: {file}: use the launcher branch agent/ instead of fix/other\n"
            f"OK: {file}\n"
        ),
        "",
    )


def test_registered() -> None:
    assert alpha.COMMANDS["brief"] is brief.main
    assert (
        "\n  brief        new | check: peer-run briefs with required safeguards\n"
        in (alpha.USAGE)
    )


def test_render_defaults() -> None:
    assert brief.render("demo", "tbhb-dev/tools") == EXPECTED
    assert brief.check(FILLED) == []
    assert brief.check(EXPECTED) == [
        "unfilled placeholder: {TASK}",
        "unfilled placeholder: {TASK_DETAILS}",
        "unfilled placeholder: {GATES}",
        "unfilled placeholder: {FINAL_REPORT}",
    ]


def test_render_overrides_and_quotes_arguments() -> None:
    rendered = brief.render(
        "a b",
        "other/project",
        "feature/x y",
        "base ref",
        scan_command=SCAN_COMMAND,
    )
    assert rendered == EXPECTED.replace("tbhb-dev/tools", "other/project").replace(
        ", demo)", ", a b)"
    ).replace(
        "git worktree add .worktrees/demo -b agent/demo origin/main",
        "git worktree add '.worktrees/a b' -b 'feature/x y' 'base ref'",
    ).replace("cd .worktrees/demo", "cd '.worktrees/a b'").replace(
        brief.NO_PUSH_BLOCK,
        PUSH_SECTION,
    )


@pytest.mark.parametrize("push", [False, True])
def test_new_from_top_level(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, push: bool
) -> None:
    assert (
        cli.main(
            [
                "alpha",
                "brief",
                "new",
                "demo",
                "--repo",
                "tbhb-dev/tools",
                *(["--push", "--scan-command", SCAN_COMMAND] if push else []),
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert output == (
        brief.render(
            "demo", "tbhb-dev/tools", scan_command=SCAN_COMMAND if push else None
        ),
        "",
    )
    file = tmp_path / "filled.md"
    file.write_text(re.sub(r"\{[^{}]+\}", "Completed", output.out), encoding="utf-8")
    assert brief.main(["check", str(file)], PROG) == 0
    assert capsys.readouterr() == (f"OK: {file}\n", "")


def test_output_file_and_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "brief.md"
    assert (
        brief.main(
            [
                "new",
                "run",
                "--repo",
                "other/repo",
                "--branch",
                "feat/run",
                "--base",
                "origin/dev",
                "--push",
                "--scan-command",
                SCAN_COMMAND,
                "-o",
                str(file),
            ],
            PROG,
        )
        == 0
    )
    assert file.read_text(encoding="utf-8") == brief.render(
        "run",
        "other/repo",
        "feat/run",
        "origin/dev",
        scan_command=SCAN_COMMAND,
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("heading", brief.REQUIRED_HEADINGS)
def test_missing_heading(heading: str) -> None:
    assert f"missing heading: {heading}" in brief.check(FILLED.replace(heading, ""))


def test_all_problems_are_reported_in_order() -> None:
    assert brief.check("") == [
        *(f"missing heading: {heading}" for heading in brief.REQUIRED_HEADINGS),
        "Step 1 lacks a git worktree add .worktrees/ line",
        "missing push rule",
        *(f"missing hard rule: {rule}" for rule in brief.HARD_RULES_BLOCK.splitlines()),
    ]


@pytest.mark.parametrize(
    "replacement",
    ["", "echo git worktree add .worktrees/demo", "git worktree add elsewhere"],
)
def test_missing_worktree_line(replacement: str) -> None:
    text = FILLED.replace(
        "git worktree add .worktrees/demo -b agent/demo origin/main", replacement
    )
    assert brief.check(text) == ["Step 1 lacks a git worktree add .worktrees/ line"]


def test_worktree_line_must_be_inside_step_one() -> None:
    command = "git worktree add .worktrees/demo -b agent/demo origin/main"
    text = FILLED.replace(command, "").replace("## Task\n", f"## Task\n{command}")
    assert brief.check(text) == ["Step 1 lacks a git worktree add .worktrees/ line"]


@pytest.mark.parametrize("prefix", [" \t", "- ", "* ", "+ ", "$ ", "  - "])
def test_prefixed_worktree_command(prefix: str) -> None:
    assert (
        brief.check(FILLED.replace("git worktree add", prefix + "git worktree add"))
        == []
    )


@pytest.mark.parametrize("rule", brief.HARD_RULES_BLOCK.splitlines())
def test_each_rule_required_in_its_section(rule: str) -> None:
    text = FILLED.replace(rule, "") + "\n" + rule
    assert brief.check(text) == [
        f"missing hard rule: {rule}",
        *(
            f"forbidden instruction: {item}"
            for item in brief.FORBIDDEN
            if brief.normalize(item) in brief.normalize(rule)
        ),
    ]


def test_rule_normalizes_whitespace_and_case() -> None:
    rule = "- Touch only your worktree and `$TMPDIR`."
    wrapped = "-  Touch\tonly your\nworktree and `$TMPDIR`."
    assert brief.check(FILLED.replace(rule, wrapped)) == []
    assert brief.check(FILLED.replace(rule, rule.lower())) == []


def test_placeholders_anywhere_and_repeated() -> None:
    assert brief.check(FILLED + "\n{ONE} {TWO_WORDS}\n{ONE} {X2}\n") == [
        "unfilled placeholder: {ONE}",
        "unfilled placeholder: {TWO_WORDS}",
        "unfilled placeholder: {ONE}",
        "unfilled placeholder: {X2}",
    ]


def test_plain_braces_are_not_placeholders() -> None:
    assert (
        brief.check(
            FILLED + "\n{} {unfinished {lower} {Two} {2X} {A-B} {A B} "
            "${TMPDIR} awk '{print}'\n"
        )
        == []
    )


@pytest.mark.parametrize("forbidden", brief.FORBIDDEN)
def test_forbidden_in_task(forbidden: str) -> None:
    text = FILLED.replace("## Task\n", f"## Task\nRun {forbidden}.\n")
    # PREK_SKIP= also contains SKIP=; every matching string is reported.
    assert brief.check(text) == [
        *(
            f"forbidden instruction: {item}"
            for item in brief.FORBIDDEN
            if brief.normalize(item) in brief.normalize(forbidden)
        ),
        *{
            "prek install": [INSTALL_CONFLICT],
            "pre-commit install": [INSTALL_CONFLICT],
            "--no-verify": [BYPASS_CONFLICT],
        }.get(forbidden, []),
    ]


def test_fixed_text_exempt_but_paraphrases_are_not() -> None:
    assert brief.check(FILLED) == []
    assert brief.check(FILLED + "\nNever run prek install.\n") == [
        "forbidden instruction: prek install"
    ]
    assert brief.check(FILLED.replace("Never run `prek", "Do not run `prek")) == [
        "forbidden instruction: prek install",
        "forbidden instruction: pre-commit install",
        "forbidden instruction: tbhbdev alpha install",
    ]


def test_check_multiple_files_continues_after_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text(FILLED, encoding="utf-8")
    bad.write_text(FILLED + "{REMAINING}\nprek install\n", encoding="utf-8")
    assert brief.main(["check", str(bad), str(good)], PROG) == 1
    assert capsys.readouterr() == (
        (
            f"{bad}: unfilled placeholder: {{REMAINING}}\n"
            f"{bad}: forbidden instruction: prek install\n"
            f"{bad}: {INSTALL_CONFLICT}\nOK: {good}\n"
        ),
        "",
    )


@pytest.mark.parametrize("invalid_utf8", [False, True])
def test_unreadable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], invalid_utf8: bool
) -> None:
    file = tmp_path / "missing.md"
    if invalid_utf8:
        file.write_bytes(b"\xff")
    assert brief.main(["check", str(file)], PROG) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith(f"{file}: ")
    assert ("decode" if invalid_utf8 else "No such file") in output.out


@pytest.mark.parametrize(
    "argv", [[], ["other"], ["new", "demo"], ["new", "--repo", "a/b"], ["check"]]
)
def test_usage_errors(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        brief.main(argv, PROG)
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith(f"usage: {PROG}")


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        brief.main(["--help"], PROG)
    assert caught.value.code == 0
    assert capsys.readouterr() == (
        (
            "usage: example brief [-h] {new,check} ...\n\n"
            "Render peer-run briefs and check their fixed safeguards. "
            "The checks use text\n"
            "patterns and recognize common prohibitions. Extra lines in Hard "
            "rules are\n"
            "allowed. The checks catch omissions and common mistakes.\n\n"
            "positional arguments:\n"
            "  {new,check}\n"
            "    new        write a peer-run brief skeleton\n"
            "    check      check completed peer-run briefs\n\n"
            "options:\n"
            "  -h, --help   show this help message and exit\n"
        ),
        "",
    )


def test_sections_preserve_preamble_empty_and_repeated_headings() -> None:
    assert brief.sections(
        "preamble\n## Empty\n## Task\nfirst\n### Child\nchild\n## Task\nsecond"
    ) == {
        "": "preamble\n",
        "## Empty": "",
        "## Task": "first\nsecond\n",
        "### Child": "child\n",
    }


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "new",
            (
                "usage: example brief new [-h] --repo OWNER/REPO "
                "[--branch BRANCH] [--base REF]\n"
                "                         [--push] [--scan-command CMD] [-o FILE]\n"
                "                         RUN\n\n"
                "positional arguments:\n"
                "  RUN\n\n"
                "options:\n"
                "  -h, --help          show this help message and exit\n"
                "  --repo OWNER/REPO\n"
                "  --branch BRANCH\n"
                "  --base REF\n"
                "  --push\n"
                "  --scan-command CMD\n"
                "  -o FILE\n"
            ),
        ),
        (
            "check",
            (
                "usage: example brief check [-h] FILE [FILE ...]\n\n"
                "positional arguments:\n"
                "  FILE\n\n"
                "options:\n"
                "  -h, --help  show this help message and exit\n"
            ),
        ),
    ],
)
def test_subcommand_help(
    command: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        brief.main([command, "--help"], PROG)
    assert caught.value.code == 0
    assert capsys.readouterr() == (expected, "")


@pytest.mark.parametrize(
    "instruction",
    [
        "PRE-COMMIT INSTALL",
        "pre-commit  install",
        "pre-commit\N{NO-BREAK SPACE}install",
        "pre-commit\ninstall",
        "\uff50\uff52\uff45-\uff43\uff4f\uff4d\uff4d\uff49\uff54 install",
        "pre-commit\u2003install",
        "pre-commit\tinstall",
    ],
)
def test_normalized_forbidden_instructions(instruction: str) -> None:
    assert brief.check(FILLED + "\nRun " + instruction) == [
        "forbidden instruction: pre-commit install",
        INSTALL_CONFLICT,
    ]


@pytest.mark.parametrize(
    ("instruction", "label"),
    [
        ("git commit -n", "git commit hook bypass"),
        ("git commit -m 'change' -n", "git commit hook bypass"),
        ("git commit --no-ver", "git commit hook bypass"),
        ("git commit --no-verify"[:-2], "git commit hook bypass"),
        ("git commit --no-verify", "git commit hook bypass"),
        ("GIT COMMIT --NO-VER", "git commit hook bypass"),
        ("git push --force", "git push force option"),
        ("git push origin HEAD -f", "git push force option"),
        ("git push --force-with-lease --force", "git push force option"),
        ("git push --force-if-includes", "git push force option"),
        ("git config core.hookspath /elsewhere", "core.hooksPath"),
        ("env -u OP_TOKEN tool", "env -u op_"),
        ("tbhbdev alpha install", "tbhbdev alpha install"),
        ("tool --no-verify", "--no-verify"),
    ],
)
def test_forbidden_command_forms(instruction: str, label: str) -> None:
    expected = [
        f"forbidden instruction: {item}"
        for item in brief.FORBIDDEN
        if brief.normalize(item) in brief.normalize(instruction)
    ]
    if label not in brief.FORBIDDEN:
        expected.append(f"forbidden instruction: {label}")
    if instruction.endswith(" -n") or "--no-verify" in instruction:
        expected.append(BYPASS_CONFLICT)
    assert brief.check(FILLED + "\nRun " + instruction) == expected


@pytest.mark.parametrize(
    "instruction",
    [
        "git commit -m 'ordinary'",
        "git commit -nothing",
        "git push --follow-tags",
        "git push --force-with-lease",
        "git push --force-with-lease origin topic",
        "git push -foo",
        "git push; echo -f",
        "git commit; echo -n",
    ],
)
def test_ordinary_commands(instruction: str) -> None:
    assert brief.check(FILLED + "\n" + instruction) == []


def test_push_rule_required_exactly_once() -> None:
    missing = FILLED.replace(brief.NO_PUSH_BLOCK, "")
    assert brief.check(missing) == ["missing push rule"]
    assert brief.check(FILLED + "\n" + brief.NO_PUSH_BLOCK) == [
        "expected exactly one push rule"
    ]
    assert brief.check(FILLED + "\n" + PUSH_SECTION) == [
        "expected exactly one push rule"
    ]
    assert brief.check(missing + "\n" + PUSH_SECTION) == []
    assert brief.check(missing + "\n" + PUSH_SECTION + "\n" + brief.PUSH_BLOCK) == [
        "expected exactly one push rule"
    ]
    assert brief.check(missing + brief.NO_PUSH_BLOCK.upper().replace(" ", "\n")) == []
    assert (
        brief.check(
            missing
            + "\n## Push\n"
            + brief.PUSH_BLOCK.upper().replace(" ", "\n")
            + "\n"
            + SCAN_LINE
        )
        == []
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--push"],
        ["--scan-command", SCAN_COMMAND],
    ],
)
def test_push_and_scan_command_required_together(
    options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        brief.main(["new", "demo", "--repo", "owner/repo", *options], PROG)
    assert caught.value.code == 2
    assert capsys.readouterr() == (
        "",
        (
            f"usage: {PROG} [-h] {{new,check}} ...\n"
            f"{PROG}: error: --push and --scan-command must be used together\n"
        ),
    )


@pytest.mark.parametrize("command", ["", "scan\nnext", "scan\rnext", "scan`next"])
def test_invalid_scan_command(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        brief.main(
            [
                "new",
                "demo",
                "--repo",
                "owner/repo",
                "--push",
                "--scan-command",
                command,
            ],
            PROG,
        )
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert f"invalid SCAN COMMAND: {command!r}" in output.err


@pytest.mark.parametrize(
    "line",
    [
        "",
        "Outgoing scan command: ``",
        SCAN_LINE + "\n" + SCAN_LINE,
        "Outgoing scan command: `scan`next`",
        "Outgoing scan command: `scan\nnext`",
        "Outgoing scan command: `scan\rnext`",
        "Outgoing scan command: `scan` trailing",
        "prefix " + SCAN_LINE,
    ],
)
def test_missing_outgoing_scan_command(
    line: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = FILLED.replace(brief.NO_PUSH_BLOCK, PUSH_SECTION.replace(SCAN_LINE, line))
    assert brief.check(text) == ["missing outgoing scan command"]
    file = tmp_path / "push.md"
    file.write_text(text, encoding="utf-8")
    assert brief.main(["check", str(file)], PROG) == 1
    assert capsys.readouterr() == (f"{file}: missing outgoing scan command\n", "")


def test_push_section_requires_fixed_block_and_its_own_command() -> None:
    text = FILLED.replace(brief.NO_PUSH_BLOCK, "## Push\n" + SCAN_LINE)
    assert brief.check(text) == [
        "missing push rule",
        "missing push rule in Push section",
    ]
    assert brief.check(text + "\n## Other\n" + brief.PUSH_BLOCK) == [
        "missing push rule in Push section"
    ]
    text = FILLED.replace(brief.NO_PUSH_BLOCK, "## Push\n" + brief.PUSH_BLOCK)
    assert brief.check(text + "\n## Other\n" + SCAN_LINE) == [
        "missing outgoing scan command"
    ]


def test_scan_command_preserved(capsys: pytest.CaptureFixture[str]) -> None:
    command = "  scan-outgoing 'origin/main' && echo $RESULT  "
    assert (
        brief.main(
            [
                "new",
                "demo",
                "--repo",
                "owner/repo",
                "--push",
                "--scan-command",
                command,
            ],
            PROG,
        )
        == 0
    )
    output = capsys.readouterr()
    assert output.err == ""
    assert brief.sections(output.out)["## Push"] == (
        f"\n{brief.PUSH_BLOCK}\nOutgoing scan command: `{command}`\n\n"
    )
    assert brief.check(re.sub(r"\{[^{}]+\}", "Completed", output.out)) == []


@pytest.mark.parametrize("heading", ["## Task", "## Gates", "## Final report"])
def test_fixed_sentence_in_other_sections_is_checked(heading: str) -> None:
    rule = brief.HARD_RULES_BLOCK.splitlines()[2]
    text = FILLED.replace(heading, heading + "\nDo this: " + rule)
    assert brief.check(text) == ["forbidden instruction: tmux -L agents"]


def test_rewrapped_fixed_text_and_extra_hard_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = FILLED
    for block in (brief.HOOK_RULE, brief.COMMITS_BLOCK, brief.HARD_RULES_BLOCK):
        text = text.replace(block, block.upper().replace(" ", "\n"))
    text = text.replace(
        "## Final report", "- An extra rule is allowed.\n## Final report"
    )
    assert brief.check(text) == []
    file = tmp_path / "wrapped.md"
    file.write_text(text, encoding="utf-8")
    assert brief.main(["check", str(file)], PROG) == 0
    assert capsys.readouterr() == (f"OK: {file}\n", "")


@pytest.mark.parametrize("fence", ["```", "~~~", "````", "~~~~"])
def test_headings_inside_fences(fence: str) -> None:
    text = FILLED.replace("```", fence)
    text = text.replace("git fetch origin", "# note\ngit fetch origin")
    assert "# note" not in brief.sections(text)
    assert "# note\n" in brief.sections(text)[brief.REQUIRED_HEADINGS[0]]
    assert brief.check(text) == []


def test_fence_must_close_with_matching_marker() -> None:
    assert brief.sections("## Task\n```sh\n~~~\n# inside\n```\n## Gates\nok") == {
        "## Task": "```sh\n~~~\n# inside\n```\n",
        "## Gates": "ok\n",
    }
    assert brief.sections("## Task\n````\n```\n# inside\n`````\n# outside") == {
        "## Task": "````\n```\n# inside\n`````\n",
        "# outside": "",
    }


@pytest.mark.parametrize(
    ("option", "value", "label"),
    [
        (None, "../run", "RUN"),
        (None, "run/name", "RUN"),
        (None, "_run", "RUN"),
        (None, "", "RUN"),
        (None, "run\n", "RUN"),
        ("--repo", "owner", "REPO"),
        ("--repo", "owner/repo/extra", "REPO"),
        ("--repo", "own_er/repo", "REPO"),
        ("--repo", "owner/", "REPO"),
        ("--repo", "owner/repo\n", "REPO"),
        ("--branch", "feat name", "BRANCH"),
        ("--branch", "feat\tname", "BRANCH"),
        ("--branch", "feat{TASK}", "BRANCH"),
        ("--branch", "feat}", "BRANCH"),
        ("--branch", "", "BRANCH"),
        ("--base", "origin/main\n", "BASE"),
        ("--base", "origin/{BASE}", "BASE"),
        ("--base", "origin/}", "BASE"),
        ("--base", "", "BASE"),
    ],
)
def test_new_validates_inputs(
    option: str | None, value: str, label: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["new", "demo", "--repo", "owner/repo"]
    if option is None:
        argv[1] = value
    elif option == "--repo":
        argv[3] = value
    else:
        argv.extend([option, value])
    with pytest.raises(SystemExit) as caught:
        brief.main(argv, PROG)
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert f"invalid {label}: {value!r}" in output.err


def test_new_accepts_valid_input_characters(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "new",
        "A0.a_-Z",
        "--repo",
        "Ow-9/R0.a_-Z",
        "--branch",
        "feat/run",
        "--base",
        "HEAD~1",
    ]
    assert brief.main(argv, PROG) == 0
    assert capsys.readouterr() == (
        brief.render("A0.a_-Z", "Ow-9/R0.a_-Z", "feat/run", "HEAD~1"),
        "",
    )


def test_new_missing_output_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "missing" / "brief.md"
    with pytest.raises(SystemExit) as caught:
        brief.main(["new", "demo", "--repo", "owner/repo", "-o", str(file)], PROG)
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "No such file" in output.err
    assert str(file) in output.err


def test_normalize_unicode_whitespace_and_case() -> None:
    assert (
        brief.normalize(
            "  \uff26u\N{LATIN SMALL LETTER SHARP S}\N{NO-BREAK SPACE}\n\t X  "
        )
        == "fuss x"
    )
    assert brief.normalize("") == ""
