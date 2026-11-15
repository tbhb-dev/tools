import json
from datetime import UTC, datetime

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import pr_stats
from tbhbdev.alpha import pull_request as pr

H = "sha-256-64:"
EARLY, MID, LATE = (
    "2026-11-14T00:00:00Z",
    "2026-11-14T01:00:00Z",
    "2026-11-14T02:00:00Z",
)


def line(key: str = "Generated-by", start: str | None = None) -> str:
    stamp = f";started_at={start}" if start else ""
    return (
        f"{key}: LLM harness=claude-code;model=anthropic:m;session={H}{'a' * 16}{stamp}"
    )


def commit(date: str, body: str = "") -> dict[str, object]:
    return {"committedDate": date, "messageBody": body}


def pull(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "number": 7,
        "state": "MERGED",
        "mergedAt": LATE,
        "closedAt": LATE,
        "body": "",
        "commits": [commit(MID)],
    }
    return base | fields


@pytest.mark.parametrize(
    ("value", "good"),
    [
        (EARLY, True),
        ("2026-02-30T00:00:00Z", False),
        ("2026-11-14T00:00:00", False),
        ("2026-11-14T00:00:00+00:00", False),
        (None, False),
        (5, False),
    ],
)
def test_parse_time(value: object, good: bool) -> None:
    assert (pr_stats.parse_time(value) is not None) is good


def test_parse_time_returns_the_exact_utc_instant() -> None:
    assert pr_stats.parse_time(EARLY) == datetime(2026, 11, 14, tzinfo=UTC)


def test_line_starts_counts_lines_without_one() -> None:
    text = f"x\n\n{line(start=MID)}\n{line('Assisted-by')}\n{line(start='bad')}\n"
    starts, missing = pr_stats.line_starts(text)
    assert [s.hour for s in starts] == [1]
    assert missing == 2
    assert pr_stats.line_starts(None) == ([], 0)


def test_an_agent_start_before_the_first_commit_wins() -> None:
    got = pr_stats.row("o/r", pull(body=line("Assisted-by", EARLY)))
    assert got == {
        "repo": "o/r",
        "number": 7,
        "state": "merged",
        "start": EARLY,
        "start_source": "started_at",
        "end": LATE,
        "cycle_seconds": 7200,
        "lines_without_started_at": 0,
    }


def test_an_older_commit_sets_the_start() -> None:
    got = pr_stats.row(
        "o/r", pull(commits=[commit(EARLY), commit(MID, line(start=MID))])
    )
    assert got is not None
    assert (got["start"], got["start_source"]) == (EARLY, "commit")


def test_a_tie_goes_to_started_at() -> None:
    got = pr_stats.row("o/r", pull(commits=[commit(MID, line(start=MID))]))
    assert got is not None
    assert got["start_source"] == "started_at"


def test_the_earliest_of_several_started_at_times_wins() -> None:
    got = pr_stats.row(
        "o/r",
        pull(body=line(start=MID), commits=[commit(LATE, line(start=EARLY))]),
    )
    assert got is not None
    assert (got["start"], got["start_source"]) == (EARLY, "started_at")


def test_a_merged_pull_request_ends_at_merged_at_not_closed_at() -> None:
    got = pr_stats.row(
        "o/r", pull(mergedAt=MID, closedAt=LATE, commits=[commit(EARLY)])
    )
    assert got is not None
    assert (got["end"], got["cycle_seconds"]) == (MID, 3600)


def test_started_at_must_stand_alone_as_an_attribute() -> None:
    text = f"{line()};late_started_at={EARLY}"
    assert pr_stats.line_starts(text) == ([], 1)


def test_an_empty_started_at_does_not_hide_a_later_one() -> None:
    starts, missing = pr_stats.line_starts(f"{line()};started_at=;started_at={EARLY}")
    assert ([s.hour for s in starts], missing) == ([0], 0)


def test_no_trailers_falls_back_to_the_earliest_commit() -> None:
    got = pr_stats.row("o/r", pull(commits=[commit(MID), commit(EARLY, line())]))
    assert got is not None
    assert (got["start_source"], got["lines_without_started_at"]) == ("commit", 1)


def test_missing_starts_accumulate_across_body_and_commits() -> None:
    got = pr_stats.row(
        "o/r",
        pull(
            body=line(),
            commits=[commit(MID, line()), commit(EARLY, line("Assisted-by"))],
        ),
    )
    assert got is not None
    assert got["lines_without_started_at"] == 3


def test_a_closed_pull_request_ends_at_closed_at() -> None:
    got = pr_stats.row("o/r", pull(state="CLOSED", mergedAt=None, closedAt=LATE))
    assert got is not None
    assert (got["state"], got["end"], got["cycle_seconds"]) == ("closed", LATE, 3600)


def test_a_started_at_alone_gives_a_start() -> None:
    got = pr_stats.row("o/r", pull(commits=None, body=line(start=EARLY)))
    assert got is not None
    assert got["start_source"] == "started_at"


@pytest.mark.parametrize(
    "data",
    [pull(state="OPEN", mergedAt=None, closedAt=None), pull(commits=[], body="")],
)
def test_rows_without_an_end_or_a_start_are_skipped(data: dict[str, object]) -> None:
    assert pr_stats.row("o/r", data) is None


def test_main_prints_a_row_per_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[list[str]] = []

    def listed(argv: list[str]) -> list[dict[str, object]]:
        seen.append(list(argv))
        return [
            pull(),
            pull(number=8, state="OPEN", mergedAt=None, closedAt=None),
            pull(number=9, commits=[]),
        ]

    monkeypatch.setattr(pr, "gh_json", listed)
    assert pr_stats.main(["o/r", "--limit", "5"], "prog") == 0
    out = capsys.readouterr()
    (printed,) = [json.loads(x) for x in out.out.splitlines()]
    assert (printed["number"], printed["repo"]) == (7, "o/r")
    assert out.err == "prog: no start for #9\n"
    assert seen == [
        [
            "pr",
            "list",
            "--repo",
            "o/r",
            "--state",
            "all",
            "--limit",
            "5",
            "--json",
            pr_stats.FIELDS,
        ]
    ]
    assert pr_stats.main(["o/r"], "prog") == 0
    assert seen[1][7] == "200"


def test_help_describes_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        pr_stats.main(["--help"], "stats")
    assert stopped.value.code == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out.startswith("usage: stats ")
    lines = [" ".join(value.split()) for value in out.out.splitlines()]
    assert "repo OWNER/REPO" in lines
    assert "--limit LIMIT pull requests to read" in lines
    assert "cycle time of merged and closed pull requests." in " ".join(lines)


def test_limit_requires_an_integer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def listed(_argv: list[str]) -> list[dict[str, object]]:
        pytest.fail("invalid limits must not query pull requests")

    monkeypatch.setattr(pr, "gh_json", listed)
    with pytest.raises(SystemExit) as stopped:
        pr_stats.main(["o/r", "--limit", "many"], "stats")
    assert stopped.value.code == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "stats: error: argument --limit: invalid int value: 'many'" in out.err


def test_the_command_runs_the_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake(argv: list[str], prog: str) -> int:
        calls.append((argv, prog))
        return 7

    monkeypatch.setattr(pr_stats, "main", fake)
    assert alpha.COMMANDS["pr-stats"] is alpha.pr_stats_command
    assert alpha.pr_stats_command(["o/r"], "prog") == 7
    assert calls == [(["o/r"], "prog")]
