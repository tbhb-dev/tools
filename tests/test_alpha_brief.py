"""Briefs carry one fixed preamble and reject unsafe instructions literally."""

import re
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha, cli
from tbhbdev.alpha import brief

if TYPE_CHECKING:
    from pathlib import Path

EXPECTED = """# {TASK} (tbhb-dev/tools, demo)

## Step 1: worktree first (required)

Before anything else, in the tbhb-dev/tools main checkout:

```sh
git fetch origin
git worktree add .worktrees/demo -b agent/demo origin/main
cd .worktrees/demo
```

If the worktree or branch already exists, stop and report.
In the main checkout, make NO commit, checkout, switch, reset, stash or file edit.
Do ALL further work in the new worktree.

```sh
uv sync  # Drop this line when the repo has no pyproject.toml.
bazelisk run //:bazel_env
```

Never run `prek install`, `pre-commit install`, `tbhbdev alpha install`, or any other hook installer.

## Task

{TASK_DETAILS}

## Gates

{GATES}

## Commits

Use plain `git commit` and let the hooks run. Never use `--no-verify`, `SKIP=`, `PREK_SKIP=`, `-c core.hooksPath=`, or any other skip or redirect of hooks. No `-s`, no Signed-off-by, no Co-authored-by.

Do NOT push and do NOT open a pull request.

## Hard rules

- After any denial, stop and report. No workaround by any tool, even an equivalent one.
- GitHub state comes only from awrel events. No gh polling, no sleep or pgrep wait loops.
- Never pkill, killall or kill by pattern; kill only a PID you started. Never connect to the `tmux -L agents` server or any agents socket.
- Never unset or export the 1Password service-account token variable. No `env`, `printenv`, `set` or `export -p` dumps; no credential helpers; don't read SSH keys, the Codex home, GitHub CLI config, or any token file.
- Never `rm`; move leftovers with `mv` into `$TMPDIR`. No ps, pgrep, top or sysctl; load checks use `uptime` only.
- No "core" or "shell" as a module or pattern name. Never write a public IP address. Agents don't open GitHub issues; follow-ups go in a private drafts file.
- Touch only your worktree and `$TMPDIR`.

## Final report

{FINAL_REPORT}
"""  # noqa: E501 -- exact output, including the required rule lines
FILLED = re.sub(r"\{[^{}]+\}", "Completed", EXPECTED)
PROG = "example brief"


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
        "a b", "other/project", "feature/x y", "base ref", push=True
    )
    assert rendered == EXPECTED.replace("tbhb-dev/tools", "other/project").replace(
        ", demo)", ", a b)"
    ).replace(
        "git worktree add .worktrees/demo -b agent/demo origin/main",
        "git worktree add '.worktrees/a b' -b 'feature/x y' 'base ref'",
    ).replace("cd .worktrees/demo", "cd '.worktrees/a b'").replace(
        "Do NOT push and do NOT open a pull request.",
        "## Push\n\nBefore any `git push`, run "
        "`~/.local/libexec/tbhb/2026-11-11-coord-scan-outgoing.sh`. "
        "Stop and report on its exit 1. Never force-push an existing remote branch.",
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
                *(["--push"] if push else []),
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert output == (brief.render("demo", "tbhb-dev/tools", push=push), "")
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
                "-o",
                str(file),
            ],
            PROG,
        )
        == 0
    )
    assert file.read_text(encoding="utf-8") == brief.render(
        "run", "other/repo", "feat/run", "origin/dev", push=True
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("heading", brief.REQUIRED_HEADINGS)
def test_missing_heading(heading: str) -> None:
    assert f"missing heading: {heading}" in brief.check(FILLED.replace(heading, ""))


def test_all_problems_are_reported_in_order() -> None:
    assert brief.check("") == [
        *(f"missing heading: {heading}" for heading in brief.REQUIRED_HEADINGS),
        "Step 1 lacks a git worktree add .worktrees/ line",
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
    text = FILLED.replace("git worktree add", "## Task\ngit worktree add")
    assert brief.check(text) == ["Step 1 lacks a git worktree add .worktrees/ line"]


def test_indented_worktree_command() -> None:
    assert brief.check(FILLED.replace("git worktree add", " \tgit worktree add")) == []


@pytest.mark.parametrize("rule", brief.HARD_RULES_BLOCK.splitlines())
def test_each_rule_required_in_its_section(rule: str) -> None:
    text = FILLED.replace(rule, "") + "\n" + rule
    assert brief.check(text) == [f"missing hard rule: {rule}"]


def test_rule_normalizes_whitespace_but_not_case() -> None:
    rule = "- Touch only your worktree and `$TMPDIR`."
    wrapped = "-  Touch\tonly your\nworktree and `$TMPDIR`."
    assert brief.check(FILLED.replace(rule, wrapped)) == []
    assert brief.check(FILLED.replace(rule, rule.lower())) == [
        f"missing hard rule: {rule}"
    ]


def test_placeholders_anywhere_and_repeated() -> None:
    assert brief.check(FILLED + "\n{one} {two words}\n{one} {x\ny}\n") == [
        "unfilled placeholder: {one}",
        "unfilled placeholder: {two words}",
        "unfilled placeholder: {one}",
        "unfilled placeholder: {x\ny}",
    ]


def test_plain_braces_are_not_placeholders() -> None:
    assert brief.check(FILLED + "\n{} {unfinished\n") == []


@pytest.mark.parametrize("forbidden", brief.FORBIDDEN)
def test_forbidden_in_task(forbidden: str) -> None:
    text = FILLED.replace("## Task\n", f"## Task\nRun {forbidden}.\n")
    # PREK_SKIP= also contains SKIP=; every matching string is reported.
    assert brief.check(text) == [
        f"forbidden instruction: {item}"
        for item in brief.FORBIDDEN
        if item in forbidden
    ]


def test_fixed_text_exempt_but_paraphrases_are_not() -> None:
    assert brief.check(FILLED) == []
    assert brief.check(FILLED + "\nNever run prek install.\n") == [
        "forbidden instruction: prek install"
    ]
    assert brief.check(FILLED.replace("Never run `prek", "Do not run `prek")) == [
        "forbidden instruction: prek install",
        "forbidden instruction: pre-commit install",
    ]


def test_check_multiple_files_continues_after_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text(FILLED, encoding="utf-8")
    bad.write_text(FILLED + "{remaining}\nprek install\n", encoding="utf-8")
    assert brief.main(["check", str(bad), str(good)], PROG) == 1
    assert capsys.readouterr() == (
        (
            f"{bad}: unfilled placeholder: {{remaining}}\n"
            f"{bad}: forbidden instruction: prek install\nOK: {good}\n"
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
            "Render peer-run briefs and check their fixed safeguards. Forbidden\n"
            "instructions use literal substring checks after removing the exact fixed\n"
            "prohibition sentences. This deliberately does not interpret English:\n"
            "paraphrases and additional mentions fail, "
            "even when they sound prohibitive.\n\n"
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
                "                         [--push] [-o FILE]\n"
                "                         RUN\n\n"
                "positional arguments:\n"
                "  RUN\n\n"
                "options:\n"
                "  -h, --help         show this help message and exit\n"
                "  --repo OWNER/REPO\n"
                "  --branch BRANCH\n"
                "  --base REF\n"
                "  --push\n"
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
