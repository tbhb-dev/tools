# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha chase-context, with a producer script in a temp dir."""

import io
import json
import os
import stat
import sys
import time
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import chase_context, gh_shim

if TYPE_CHECKING:
    from pathlib import Path

ROW_A = "CHASE  repo-a#1 abcd1234 25m draft=false user | one"
ROW_B = "CHASE  repo-b#2 abcd1234 30m draft=true user | two"
OLD = "CHASE  repo-a#3 abcd1234 1440m draft=false user | old"
RENOVATE = "CHASE  repo-a#4 abcd1234 30m draft=false renovate[bot] | pins"
OK_ROW = "ok     repo-a#5 abcd1234 2m draft=false user | fresh"
HEADER = chase_context.HEADER


@pytest.fixture(autouse=True)
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty state directory, no project directory and an empty hook input."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return tmp_path / "state" / "tbhbdev"


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "producer.sh"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def echo(*lines: str) -> str:
    return "\n".join(f"echo '{line}'" for line in lines)


def configure(config_file: Path, producer: Path, repos: str = "") -> None:
    config_file.write_text(
        f'[harness]\nwatchdog_producer = "{producer}"\n{repos}', encoding="utf-8"
    )


def set_cache_age(state: Path, seconds: float) -> None:
    cache = state / "chase-context.cache"
    stamp = time.time() - seconds
    os.utime(cache, (stamp, stamp))


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        (ROW_A, True),
        ("CHASE repo#1 abc 1439m x", True),
        (OLD, False),
        (RENOVATE, False),
        ("CHASE  repo-a#6 abc 30m draft=false Renovate[bot] | PINS", False),
        (OK_ROW, False),
        ("CHASE repo-a#7 abc soon draft=false", False),
        ("chase repo-a#8 abc 30m", False),
        ("CHASE Repo#9 abc 30m x", False),
        ("", False),
    ],
)
def test_keep_passes_only_a_young_well_formed_row_not_from_renovate(
    line: str, kept: bool
) -> None:
    assert chase_context.keep(line) is kept


def test_produce_returns_the_kept_rows(tmp_path: Path) -> None:
    producer = script(tmp_path, echo(ROW_A, OK_ROW, OLD, RENOVATE, ROW_B))
    assert chase_context.produce(str(producer)) == [ROW_A, ROW_B]


def test_produce_puts_the_shim_directory_first_on_path(tmp_path: Path) -> None:
    shim = gh_shim.bin_dir()
    producer = script(tmp_path, f'case "$PATH" in "{shim}":*) echo \'{ROW_A}\' ;; esac')
    assert chase_context.produce(str(producer)) == [ROW_A]


def test_produce_runs_a_tilde_path_from_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(tmp_path, echo(ROW_A))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert chase_context.produce("~/producer.sh") == [ROW_A]


def test_produce_gives_nothing_for_a_missing_producer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert chase_context.produce(str(tmp_path / "missing.sh")) == []
    assert "the producer failed or timed out" in capsys.readouterr().err


def test_produce_gives_nothing_for_a_producer_that_hangs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(chase_context, "PRODUCER_SECONDS", 0.2)
    producer = script(tmp_path, "sleep 5")
    assert chase_context.produce(str(producer)) == []
    assert "timed out" in capsys.readouterr().err


def test_rows_runs_the_producer_and_caches_every_kept_row(
    tmp_path: Path, state: Path
) -> None:
    producer = script(tmp_path, echo(ROW_A, OLD, ROW_B))
    assert chase_context.rows(str(producer)) == [ROW_A, ROW_B]
    assert (state / "chase-context.cache").read_text() == f"{ROW_A}\n{ROW_B}\n"


def test_rows_reads_a_fresh_cache_without_running_the_producer(
    tmp_path: Path, state: Path
) -> None:
    state.mkdir(parents=True)
    (state / "chase-context.cache").write_text(f"{ROW_B}\n")
    set_cache_age(state, 599)
    assert chase_context.rows(str(tmp_path / "missing.sh")) == [ROW_B]


def test_rows_reads_an_empty_cache_as_no_rows(tmp_path: Path, state: Path) -> None:
    state.mkdir(parents=True)
    (state / "chase-context.cache").write_text("")
    assert chase_context.rows(str(tmp_path / "missing.sh")) == []


def test_rows_runs_the_producer_again_once_the_cache_is_ten_minutes_old(
    tmp_path: Path, state: Path
) -> None:
    state.mkdir(parents=True)
    (state / "chase-context.cache").write_text(f"{ROW_B}\n")
    set_cache_age(state, 601)
    producer = script(tmp_path, echo(ROW_A))
    assert chase_context.rows(str(producer)) == [ROW_A]
    assert (state / "chase-context.cache").read_text() == f"{ROW_A}\n"


def test_rows_caches_a_failed_run_as_no_rows(tmp_path: Path, state: Path) -> None:
    assert chase_context.rows(str(tmp_path / "missing.sh")) == []
    assert (state / "chase-context.cache").read_text() == ""


@pytest.mark.parametrize(
    ("core", "runs"),
    [
        ('{"remaining": 0}', False),
        ('{"remaining": 7}', True),
        ("{}", True),
        ("not json", True),
        (None, True),
    ],
)
def test_rows_stays_idle_while_the_budget_has_nothing_left(
    tmp_path: Path, core: str | None, *, runs: bool
) -> None:
    if core is not None:
        gh_shim.state_dir().mkdir(parents=True)
        (gh_shim.state_dir() / gh_shim.CORE).write_text(core)
    producer = script(tmp_path, echo(ROW_A))
    assert chase_context.rows(str(producer)) == ([ROW_A] if runs else None)


def test_project_dir_prefers_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/x/from-env")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"cwd": "/x/from-input"}'))
    assert chase_context.project_dir() == "/x/from-env"


@pytest.mark.parametrize(
    ("stdin", "expected"),
    [
        ('{"cwd": "/x/from-input"}', "/x/from-input"),
        ('{"cwd": 3}', None),
        ("{}", None),
        ("[1]", None),
        ("not json", None),
        ("", None),
    ],
)
def test_project_dir_falls_back_to_the_hook_input(
    monkeypatch: pytest.MonkeyPatch, stdin: str, expected: str | None
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert chase_context.project_dir() == expected


@pytest.mark.parametrize(
    ("entries", "project", "expected"),
    [
        (["p=repo-a, repo-b"], "/x/p", {"repo-a", "repo-b"}),
        (["q=repo-c", " p = repo-a "], "/x/p/", {"repo-a"}),
        (["p=repo-a"], "/x/other", None),
        (["p=repo-a"], "/x/p/sub", None),
        (["p=repo-a"], None, None),
        (["p=repo-a"], "", None),
        (None, "/x/p", None),
        ([], "/x/p", None),
        (["p=repo-a", "p-docs=repo-b"], "/x/p-docs", {"repo-b"}),
        (["p-docs=repo-b", "p=repo-a"], "/x/p-docs-fix", {"repo-b"}),
        (["p=repo-a"], "/x/p-feature", {"repo-a"}),
        (["p=repo-a", "p=repo-b"], "/x/p", {"repo-a"}),
        (["p=repo-a"], "/x/pq", None),
    ],
)
def test_own_repos_picks_the_entry_for_the_project_directory_name(
    entries: list[str] | None, project: str | None, expected: set[str] | None
) -> None:
    assert chase_context.own_repos(entries, project) == expected


def test_main_prints_the_header_and_only_the_sessions_rows(
    tmp_path: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(
        config_file,
        script(tmp_path, echo(ROW_A, ROW_B)),
        'watchdog_repos = ["one=repo-a", "two=repo-b"]\n',
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/x/two")
    assert chase_context.main() == 0
    assert capsys.readouterr() == (f"{HEADER}\n{ROW_B}\n", "")


def test_main_prints_every_row_to_a_session_with_no_entry(
    tmp_path: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(
        config_file,
        script(tmp_path, echo(ROW_A, ROW_B)),
        'watchdog_repos = ["one=repo-a"]\n',
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": "/x/none"})))
    assert chase_context.main() == 0
    assert capsys.readouterr().out == f"{HEADER}\n{ROW_A}\n{ROW_B}\n"


def test_main_prints_nothing_when_no_row_belongs_to_the_session(
    tmp_path: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(
        config_file,
        script(tmp_path, echo(ROW_A)),
        'watchdog_repos = ["two=repo-b"]\n',
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/x/two")
    assert chase_context.main() == 0
    assert capsys.readouterr() == ("", "")


def test_main_prints_nothing_while_the_budget_is_spent(
    tmp_path: Path, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(config_file, script(tmp_path, echo(ROW_A)))
    gh_shim.state_dir().mkdir(parents=True)
    (gh_shim.state_dir() / gh_shim.CORE).write_text('{"remaining": 0}')
    assert chase_context.main() == 0
    assert capsys.readouterr() == ("", "")


def test_main_prints_nothing_without_a_producer_or_a_config(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert chase_context.main() == 0
    config_file.write_text('[harness]\nwatchdog_repos = ["one=repo-a"]\n')
    assert chase_context.main() == 0
    assert capsys.readouterr() == ("", "")


def test_main_fails_open_on_a_broken_config(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text("[harness\n")
    assert chase_context.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("chase-context: ")


def test_main_fails_open_on_output_that_is_not_utf8(
    tmp_path: Path, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(config_file, script(tmp_path, "printf '\\377\\376\\n'"))
    assert chase_context.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("chase-context: ")


def test_main_fails_open_when_the_producer_hangs(
    tmp_path: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(chase_context, "PRODUCER_SECONDS", 0.2)
    configure(config_file, script(tmp_path, "sleep 5"))
    assert chase_context.main() == 0
    assert capsys.readouterr().out == ""


def test_main_fails_open_when_the_producer_is_missing(
    tmp_path: Path, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(config_file, tmp_path / "missing.sh")
    assert chase_context.main() == 0
    assert capsys.readouterr().out == ""


def test_main_fails_open_when_the_cache_is_unreadable(
    tmp_path: Path,
    config_file: Path,
    state: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(config_file, script(tmp_path, echo(ROW_A)))
    (state / "chase-context.cache").mkdir(parents=True)
    assert chase_context.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("chase-context: ")
