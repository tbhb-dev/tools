"""Render peer-run briefs and check their fixed safeguards.

Forbidden instructions use literal substring checks after removing the exact
fixed prohibition sentences. This deliberately does not interpret English:
paraphrases and additional mentions fail, even when they sound prohibitive.
"""

import argparse
import re
import shlex
from collections import defaultdict
from pathlib import Path

HOOK_RULE = (
    "Never run `prek install`, `pre-commit install`, `tbhbdev alpha install`, "
    "or any other hook installer."
)
STEP_BLOCK = """## Step 1: worktree first (required)

Before anything else, in the {repo} main checkout:

```sh
git fetch origin
git worktree add {path} -b {branch} {base}
cd {path}
```

If the worktree or branch already exists, stop and report.
In the main checkout, make NO commit, checkout, switch, reset, stash or file edit.
Do ALL further work in the new worktree.

```sh
uv sync  # Drop this line when the repo has no pyproject.toml.
bazelisk run //:bazel_env
```

{hook_rule}"""
COMMITS_BLOCK = (
    "Use plain `git commit` and let the hooks run. "
    "Never use `--no-verify`, `SKIP=`, `PREK_SKIP=`, `-c core.hooksPath=`, "
    "or any other skip or redirect of hooks. "
    "No `-s`, no Signed-off-by, no Co-authored-by."
)
PUSH_BLOCK = (
    "Before any `git push`, run "
    "`~/.local/libexec/tbhb/2026-11-11-coord-scan-outgoing.sh`. "
    "Stop and report on its exit 1. Never force-push an existing remote branch."
)
NO_PUSH_BLOCK = "Do NOT push and do NOT open a pull request."
HARD_RULES_BLOCK = """- After any denial, stop and report. No workaround by any tool, even an equivalent one.
- GitHub state comes only from awrel events. No gh polling, no sleep or pgrep wait loops.
- Never pkill, killall or kill by pattern; kill only a PID you started. Never connect to the `tmux -L agents` server or any agents socket.
- Never unset or export the 1Password service-account token variable. No `env`, `printenv`, `set` or `export -p` dumps; no credential helpers; don't read SSH keys, the Codex home, GitHub CLI config, or any token file.
- Never `rm`; move leftovers with `mv` into `$TMPDIR`. No ps, pgrep, top or sysctl; load checks use `uptime` only.
- No "core" or "shell" as a module or pattern name. Never write a public IP address. Agents don't open GitHub issues; follow-ups go in a private drafts file.
- Touch only your worktree and `$TMPDIR`."""  # noqa: E501 -- fixed rule lines
REQUIRED_HEADINGS = (
    "## Step 1: worktree first (required)",
    "## Hard rules",
    "## Final report",
)
PROHIBITION_REPLACEMENT = ""
FORBIDDEN = (
    "prek install",
    "pre-commit install",
    "--no-verify",
    "SKIP=",
    "PREK_SKIP=",
    "core.hooksPath",
    "tmux -L agents",
    "follow-up issue",
    "gh issue create",
    "unset OP_",
)


def render(
    run: str,
    repo: str,
    branch: str | None = None,
    base: str = "origin/main",
    *,
    push: bool = False,
) -> str:
    """Return a skeleton; callers fill every brace placeholder before checking."""
    step = STEP_BLOCK.format(
        repo=repo,
        path=shlex.quote(f".worktrees/{run}"),
        branch=shlex.quote(branch or f"agent/{run}"),
        base=shlex.quote(base),
        hook_rule=HOOK_RULE,
    )
    return "\n\n".join(
        (
            f"# {{TASK}} ({repo}, {run})",
            step,
            "## Task\n\n{TASK_DETAILS}",
            "## Gates\n\n{GATES}",
            f"## Commits\n\n{COMMITS_BLOCK}",
            f"## Push\n\n{PUSH_BLOCK}" if push else NO_PUSH_BLOCK,
            f"## Hard rules\n\n{HARD_RULES_BLOCK}",
            "## Final report\n\n{FINAL_REPORT}\n",
        )
    )


def sections(text: str) -> dict[str, str]:
    """Collect second-level Markdown sections, ending at the next heading."""
    found: defaultdict[str, str] = defaultdict(str)
    heading = ""
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line
            found.setdefault(heading, "")
        else:
            found[heading] += line + "\n"
    return found


def check(text: str) -> list[str]:
    """Return each problem, using whitespace normalization only for hard rules."""
    found = sections(text)
    problems = [
        f"missing heading: {heading}"
        for heading in REQUIRED_HEADINGS
        if heading not in found
    ]
    step = found[REQUIRED_HEADINGS[0]]
    if not any(
        line.strip().startswith("git worktree add .worktrees/")
        for line in step.splitlines()
    ):
        problems.append("Step 1 lacks a git worktree add .worktrees/ line")
    rules = " ".join(found["## Hard rules"].split())
    problems.extend(
        f"missing hard rule: {rule}"
        for rule in HARD_RULES_BLOCK.splitlines()
        if " ".join(rule.split()) not in rules
    )
    problems.extend(
        f"unfilled placeholder: {placeholder}"
        for placeholder in re.findall(r"\{[^{}]+\}", text)
    )
    remaining = text
    for fixed in (HOOK_RULE, COMMITS_BLOCK, *HARD_RULES_BLOCK.splitlines()):
        remaining = remaining.replace(fixed, PROHIBITION_REPLACEMENT)
    problems.extend(
        f"forbidden instruction: {forbidden}"
        for forbidden in FORBIDDEN
        if forbidden in remaining
    )
    return problems


def check_files(files: list[str]) -> int:
    status = 0
    for filename in files:
        try:
            problems = check(Path(filename).read_bytes().decode())
        except (OSError, UnicodeError) as exc:
            problems = [str(exc)]
        if problems:
            status = 1
            for problem in problems:
                print(f"{filename}: {problem}")
        else:
            print(f"OK: {filename}")
    return status


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="write a peer-run brief skeleton")
    new.add_argument("run", metavar="RUN")
    new.add_argument("--repo", required=True, metavar="OWNER/REPO")
    new.add_argument("--branch")
    new.add_argument("--base", default="origin/main", metavar="REF")
    new.add_argument("--push", action="store_true")
    new.add_argument("-o", metavar="FILE")
    lint = commands.add_parser("check", help="check completed peer-run briefs")
    lint.add_argument("files", nargs="+", metavar="FILE")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check_files(args.files)
    text = render(args.run, args.repo, args.branch, args.base, push=args.push)
    if args.o:
        Path(args.o).write_bytes(text.encode())
    else:
        print(text, end="")
    return 0
