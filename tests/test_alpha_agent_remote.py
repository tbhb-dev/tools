"""The remote shell protocol, pinned as literal values for the host integration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev.alpha.agent.decisions import remote
from tbhbdev.alpha.agent.decisions import runfiles_names as files

if TYPE_CHECKING:
    from collections.abc import Callable

CASES: list[tuple[str, tuple[str, ...], dict[str, bool], str | tuple[str, ...]]] = [
    (
        "session",
        ("sample", "kill-session"),
        {},
        ("tmux -L agents kill-session -t ag-sample"),
    ),
    (
        "session",
        ("sample", "has-session"),
        {},
        ("tmux -L agents has-session -t ag-sample"),
    ),
    (
        "preflight",
        ("example-org/example-repo", "refs/pull/2/head", "tbhbdev example"),
        {},
        (
            "installed=$(tbhbdev --version) || exit \"$?\"\nprintf '%s\\n' "
            '"$installed"\ntest "$installed" = \'tbhbdev example\' || exit '
            '0\ncd "$HOME"/Code/github.com/example-org/example-repo || '
            'exit "$?"\ngit -c core.hooksPath=/dev/null ls-remote '
            "--exit-code -- origin refs/pull/2/head 'refs/pull/2/head^{}'"
        ),
    ),
    (
        "transfer",
        (),
        {},
        (
            'd="${XDG_STATE_HOME:-$HOME/.local/state}/tbhbdev/agent-runs"\n'
            'umask 077\nmkdir -p "$d" && chmod 700 "$d" && tar -xf - -C '
            '"$d"'
        ),
    ),
    (
        "start",
        ("sample",),
        {},
        (
            "tmux",
            "-L",
            "agents",
            "new-session",
            "-d",
            "-s",
            "ag-sample",
            "tbhbdev",
            "alpha",
            "agent",
            "run-host",
            "sample",
        ),
    ),
    (
        "read",
        ("sample",),
        {},
        (
            'd="${XDG_STATE_HOME:-$HOME/.local/state}/tbhbdev/agent-runs"\n'
            'cd "$d" || exit "$?"\nif test -f sample.phase; then printf '
            "'%s' \"$(cat sample.phase)\"; else printf 'unknown'; fi\n"
            "printf '\\n'\nif test -f sample.rc; then printf '%s' \"$(cat "
            "sample.rc)\"; fi\nprintf '\\n'\ntest ! -f sample.launched; "
            "printf '%s\\n' \"$?\"\ntmux -L agents has-session -t ag-sample "
            '2>/dev/null; test "$?" -ne 0; printf \'%s\\n\' "$?"\nif test -f '
            "sample.log; then head -n 20 sample.log; fi\n"
        ),
    ),
    (
        "read",
        ("sample",),
        {"answer": True},
        (
            'd="${XDG_STATE_HOME:-$HOME/.local/state}/tbhbdev/agent-runs"\n'
            'cd "$d" || exit "$?"\nif test -f sample.phase; then printf '
            "'%s' \"$(cat sample.phase)\"; else printf 'unknown'; fi\n"
            "printf '\\n'\nif test -f sample.rc; then printf '%s' \"$(cat "
            "sample.rc)\"; fi\nprintf '\\n'\ntest ! -f sample.launched; "
            "printf '%s\\n' \"$?\"\ntmux -L agents has-session -t ag-sample "
            '2>/dev/null; test "$?" -ne 0; printf \'%s\\n\' "$?"\nif test -f '
            "sample.out.final; then tail -n 40 sample.out.final\nelif "
            "test -f sample.out; then tail -n 40 sample.out; fi\n"
        ),
    ),
    (
        "fallback",
        ("sample",),
        {},
        (
            'd="${XDG_STATE_HOME:-$HOME/.local/state}/tbhbdev/agent-runs"\n'
            'umask 077\ncd "$d" || exit "$?"\nif tmux -L agents '
            "has-session -t ag-sample 2>/dev/null; then exit 1; fi\nif "
            "test ! -f sample.rc; then printf '137\\n' > "
            "sample.rc.pending && mv -n sample.rc.pending sample.rc; fi\n"
        ),
    ),
    (
        "keep",
        ("sample",),
        {},
        (
            'd="${XDG_STATE_HOME:-$HOME/.local/state}/tbhbdev/agent-runs"\n'
            'umask 077\n: > "$d/sample.keep"'
        ),
    ),
    (
        "branch_facts",
        ("example-org/example-repo", "sample"),
        {},
        (
            'cd "$HOME"/Code/github.com/example-org/example-repo || exit '
            '"$?"\nif git -c core.hooksPath=/dev/null show-ref --verify '
            "--quiet refs/heads/agent/sample; then\n  if upstream=$(git "
            "-c core.hooksPath=/dev/null rev-parse --symbolic-full-name "
            "agent/sample@{upstream} 2>/dev/null); then\n    git -c "
            "core.hooksPath=/dev/null rev-list --count agent/sample "
            "\"^$upstream\"\n  else printf '1\\n'; fi\nelse printf '0\\n'; fi\n"
        ),
    ),
    (
        "clean",
        ("example-org/example-repo", "sample", "20261109T000000Z"),
        {"force": False, "delete_branch": False},
        (
            'd="${XDG_STATE_HOME:-$HOME/.local/state}/tbhbdev/agent-runs"\n'
            "if tmux -L agents has-session -t ag-sample 2>/dev/null; "
            "then exit 1; fi\ncd "
            '"$HOME"/Code/github.com/example-org/example-repo || exit '
            '"$?"\nif test -d .worktrees/agent-sample; then git -c '
            "core.hooksPath=/dev/null worktree remove "
            '.worktrees/agent-sample || exit "$?"; fi\numask 077\n'
            'trash="$d/trash/20261109T000000Z-sample"\nmkdir -p "$trash" '
            '|| exit "$?"\nif test -e "$d/sample.request.json"; then mv '
            '"$d/sample.request.json" "$trash/" || exit "$?"; fi\nif test '
            '-e "$d/sample.prompt"; then mv "$d/sample.prompt" "$trash/" '
            '|| exit "$?"; fi\nif test -e "$d/sample.schema.json"; then '
            'mv "$d/sample.schema.json" "$trash/" || exit "$?"; fi\nif '
            'test -e "$d/sample.pgid"; then mv "$d/sample.pgid" '
            '"$trash/" || exit "$?"; fi\nif test -e "$d/sample.launched"; '
            'then mv "$d/sample.launched" "$trash/" || exit "$?"; fi\nif '
            'test -e "$d/sample.phase"; then mv "$d/sample.phase" '
            '"$trash/" || exit "$?"; fi\nif test -e "$d/sample.out"; then '
            'mv "$d/sample.out" "$trash/" || exit "$?"; fi\nif test -e '
            '"$d/sample.out.final"; then mv "$d/sample.out.final" '
            '"$trash/" || exit "$?"; fi\nif test -e "$d/sample.log"; then '
            'mv "$d/sample.log" "$trash/" || exit "$?"; fi\nif test -e '
            '"$d/sample.rc"; then mv "$d/sample.rc" "$trash/" || exit '
            '"$?"; fi\nif test -e "$d/sample.keep"; then mv '
            '"$d/sample.keep" "$trash/" || exit "$?"; fi\nif test -e '
            '"$d/sample.rc.pending"; then mv "$d/sample.rc.pending" '
            '"$trash/" || exit "$?"; fi\n'
        ),
    ),
    (
        "clean",
        ("example-org/example-repo", "sample", "20261109T000000Z"),
        {"force": True, "delete_branch": True},
        (
            'd="${XDG_STATE_HOME:-$HOME/.local/state}/tbhbdev/agent-runs"\n'
            "if tmux -L agents has-session -t ag-sample 2>/dev/null; "
            "then exit 1; fi\ncd "
            '"$HOME"/Code/github.com/example-org/example-repo || exit '
            '"$?"\nif test -d .worktrees/agent-sample; then git -c '
            "core.hooksPath=/dev/null worktree remove --force "
            '.worktrees/agent-sample || exit "$?"; fi\nif git -c '
            "core.hooksPath=/dev/null show-ref --verify --quiet "
            "refs/heads/agent/sample; then\ngit -c "
            "core.hooksPath=/dev/null branch -D agent/sample || exit "
            '"$?"\nfi\numask 077\ntrash="$d/trash/20261109T000000Z-sample"\n'
            'mkdir -p "$trash" || exit "$?"\nif test -e '
            '"$d/sample.request.json"; then mv "$d/sample.request.json" '
            '"$trash/" || exit "$?"; fi\nif test -e "$d/sample.prompt"; '
            'then mv "$d/sample.prompt" "$trash/" || exit "$?"; fi\nif '
            'test -e "$d/sample.schema.json"; then mv '
            '"$d/sample.schema.json" "$trash/" || exit "$?"; fi\nif test '
            '-e "$d/sample.pgid"; then mv "$d/sample.pgid" "$trash/" || '
            'exit "$?"; fi\nif test -e "$d/sample.launched"; then mv '
            '"$d/sample.launched" "$trash/" || exit "$?"; fi\nif test -e '
            '"$d/sample.phase"; then mv "$d/sample.phase" "$trash/" || '
            'exit "$?"; fi\nif test -e "$d/sample.out"; then mv '
            '"$d/sample.out" "$trash/" || exit "$?"; fi\nif test -e '
            '"$d/sample.out.final"; then mv "$d/sample.out.final" '
            '"$trash/" || exit "$?"; fi\nif test -e "$d/sample.log"; then '
            'mv "$d/sample.log" "$trash/" || exit "$?"; fi\nif test -e '
            '"$d/sample.rc"; then mv "$d/sample.rc" "$trash/" || exit '
            '"$?"; fi\nif test -e "$d/sample.keep"; then mv '
            '"$d/sample.keep" "$trash/" || exit "$?"; fi\nif test -e '
            '"$d/sample.rc.pending"; then mv "$d/sample.rc.pending" '
            '"$trash/" || exit "$?"; fi\n'
        ),
    ),
]


@pytest.mark.parametrize(("method", "args", "kwargs", "expected"), CASES)
def test_remote_contract(
    method: str,
    args: tuple[str, ...],
    kwargs: dict[str, bool],
    expected: str | tuple[str, ...],
) -> None:
    build = cast("Callable[..., str | tuple[str, ...]]", getattr(remote, method))
    assert build(*args, **kwargs) == expected


def test_file_names() -> None:
    assert files.DIRECTORY == "tbhbdev/agent-runs"
    assert files.INDEX == "index.jsonl"
    assert files.SUFFIXES == (
        ".request.json",
        ".prompt",
        ".schema.json",
        ".pgid",
        ".launched",
        ".phase",
        ".out",
        ".out.final",
        ".log",
        ".rc",
        ".keep",
        ".rc.pending",
    )


def test_source_and_fixtures_are_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "src/tbhbdev/alpha/agent"
    paths = [
        *source.rglob("*.py"),
        root / "tests/agent_example.py",
        root / "tests/agent_driver_example.py",
    ]
    forbidden = re.compile(r"peer-(?:agent|run)\.sh|run.book|PEER_")
    for path in paths:
        assert forbidden.search(path.read_text()) is None, str(path)
