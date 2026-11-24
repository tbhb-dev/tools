"""Tests for `review pair`."""

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import agent, review, review_pair

if TYPE_CHECKING:
    from pathlib import Path

HEAD = "a" * 40
MOVED = "b" * 40
READ = review_pair.pull_request_state


def run_spec(name: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "prompt_file": f"/p/{name}.md",
        "host": "review-host",
        "repo": "tbhb-dev/tools",
        "model": "gpt-6.2-sol",
        "effort": "high",
        "head": HEAD,
        "pr": 358,
        **extra,
    }


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    return path


def pair(tmp_path: Path) -> Path:
    return write(tmp_path, {"runs": [run_spec("a"), run_spec("b", harness="agy")]})


def done(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def tool_name(name: str) -> str:
    return name


def fail_read(_argv: list[str]) -> subprocess.CompletedProcess[str]:
    return done(1, "", "no")


@pytest.fixture(autouse=True)
def pull_request(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool, str]]:
    """The PR reads return these answers in order, then repeat the last."""
    answers: list[tuple[str, bool, str]] = [(HEAD, False, "OPEN")]

    def read(_repo: str, _number: int) -> tuple[str, bool, str]:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(review_pair, "pull_request_state", read)
    return answers


def fake(
    monkeypatch: pytest.MonkeyPatch, replies: dict[str, object]
) -> list[list[str]]:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        verb = argv[len(review_pair.LAUNCHER)]
        reply = replies[f"{verb} {argv[-1] if verb == 'wait' else argv[-2]}"]
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, subprocess.CompletedProcess)
        return reply

    monkeypatch.setattr(review_pair, "run_command", run)
    return seen


ANSWER = "a: rc=0\nVERDICT: GO\n- P2 (1) x\n- P2 (2) y\n**P3** z\n\nwarning\n"


def test_each_run_prints_one_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = fake(
        monkeypatch,
        {
            "exec a": done(0),
            "wait a": done(0, ANSWER),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\n> **RECOMMEND HOLD** P1 (1) z\nP1 (2)\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 0
    out = capsys.readouterr().out
    assert "a\n  head: " + HEAD + "\n  state: rc=0\n  verdict: VERDICT: GO\n" in out
    assert (
        "  findings: P1=0 P2=2 P3=1 BLOCK=0 TECH-DEBT=0 SECURITY-DEBT=0\n"
        "  log: tbhbdev alpha agent log a\n" in out
    )
    assert "  verdict: RECOMMEND HOLD** P1 (1) z\n  findings: P1=1" in out
    first = next(argv for argv in seen if argv[-1] == "/p/a.md")
    assert first[len(review_pair.LAUNCHER) :] == [
        "exec", "--profile", "review", "--ref", HEAD, "--host", "review-host",
        "--repo", "tbhb-dev/tools", "--model", "gpt-6.2-sol",
        "--effort", "high", "a", "/p/a.md",
    ]  # fmt: skip


def test_review_format_prints_verdict_and_finding_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hold = (
        "a: rc=0\n## Verdict\n\nHOLD\n\n## Findings\n\n"
        "- BLOCK `src/app.py:4` Return the saved value.\n"
        "- BLOCK `src/app.py:8` Handle the missing record.\n"
        "- TECH-DEBT `src/app.py:12` Consolidate the lookup.\n"
        "- SECURITY-DEBT `src/app.py:16` Narrow the permission.\n"
    )
    fake(
        monkeypatch,
        {
            "exec a": done(0),
            "wait a": done(0, hold),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\n## Verdict\nGO\n\n## Findings\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 0
    out = capsys.readouterr().out
    assert (
        "  verdict: HOLD\n"
        "  findings: P1=0 P2=0 P3=0 BLOCK=2 TECH-DEBT=1 SECURITY-DEBT=1\n" in out
    )
    assert "  verdict: GO\n" in out


def test_generated_launches_satisfy_the_real_exec_parser(tmp_path: Path) -> None:
    run = run_spec(
        "a",
        pr=1,
        harness="claude",
        reason="review",
        claude_profile="y",
        account="x",
        fetch_refs=["r1", "r2"],
    )
    path = write(tmp_path, {"runs": [run, run_spec("b", pr=1, harness="codex")]})
    argv = review_pair.exec_argv(review_pair.load_spec(path)[0])[
        len(review_pair.LAUNCHER) :
    ]
    parsed = agent.parser("agent").parse_args(argv)
    assert parsed.ref == HEAD
    assert parsed.profile == "review"
    assert parsed.harness == "claude"
    assert parsed.reason == "review"
    assert parsed.claude_profile == "y"
    assert parsed.account == "x"
    assert parsed.fetch_ref == ["r1", "r2"]
    assert (parsed.name, parsed.prompt_file) == ("a", "/p/a.md")


def test_a_run_that_fails_to_launch_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake(
        monkeypatch,
        {
            "exec a": done(1, "x\n", "/home/someone/host failure\n"),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\nVERDICT: GO\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 1
    out = capsys.readouterr().out
    assert (
        f"a\n  head: {HEAD}\n  state: not launched (exit 1)\n"
        "  verdict: none\n  findings: none\n" in out
    )
    assert "/home/" not in out
    assert "b\n  head: " + HEAD + "\n  state: rc=0" in out


def test_a_lost_start_acknowledgement_still_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = fake(
        monkeypatch,
        {
            "exec a": done(75),
            "wait a": done(0, ANSWER),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\nVERDICT: GO\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 0
    assert [*review_pair.LAUNCHER, "wait", "a"] in seen
    assert "a\n  head: " + HEAD + "\n  state: rc=0" in capsys.readouterr().out


def test_a_run_that_finishes_before_launch_acknowledgement_keeps_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake(
        monkeypatch,
        {
            "exec a": done(7, "a: rc=7 sandbox=unsandboxed\nlog output\n"),
            "wait a": done(7, "a: rc=7\n## Verdict\nHOLD\n"),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\nVERDICT: GO\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 1
    assert "a\n  head: " + HEAD + "\n  state: rc=7\n  verdict: HOLD\n" in (
        capsys.readouterr().out
    )


def test_a_worker_error_keeps_the_sibling_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake(
        monkeypatch,
        {
            "exec a": OSError("boom"),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\nVERDICT: GO\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 1
    out = capsys.readouterr().out
    assert "state: error (OSError)" in out
    assert "b\n  head: " + HEAD + "\n  state: rc=0" in out


def test_an_unreachable_or_unparsed_run_is_reported_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake(
        monkeypatch,
        {
            "exec a": done(0),
            "wait a": done(1, "a: rc=unreachable\nno answer\n"),
            "exec b": done(0),
            "wait b": done(1, "", "boom\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 1
    out = capsys.readouterr().out
    assert (
        "state: rc=unreachable\n  verdict: unparsed\n  findings: P1=0 P2=0 P3=0" in out
    )
    assert "state: rc=unknown (wait exit 1)" in out
    assert "boom" not in out


def test_empty_output_has_a_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake(
        monkeypatch,
        {"exec a": done(2), "exec b": done(2)},
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 1
    assert "state: not launched (exit 2)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "state",
    [(HEAD, True, "OPEN"), (HEAD, False, "CLOSED"), (MOVED, False, "OPEN")],
)
def test_a_draft_closed_or_moved_pr_is_refused_before_any_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pull_request: list[tuple[str, bool, str]],
    state: tuple[str, bool, str],
) -> None:
    pull_request[:] = [state]
    monkeypatch.setattr(review_pair, "run_command", pytest.fail)
    assert review_pair.main([str(pair(tmp_path))], "p") == 2
    assert "must be open, ready, and at the spec head" in capsys.readouterr().err


def test_a_head_that_moves_during_the_runs_voids_the_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pull_request: list[tuple[str, bool, str]],
) -> None:
    pull_request[:] = [(HEAD, False, "OPEN"), (MOVED, False, "OPEN")]
    fake(
        monkeypatch,
        {
            "exec a": done(0),
            "wait a": done(0, ANSWER),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\nVERDICT: GO\n"),
        },
    )
    assert review_pair.main([str(pair(tmp_path))], "p") == 1
    assert f"head moved to {MOVED}" in capsys.readouterr().err


def test_an_unreadable_pr_after_the_runs_voids_the_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reads = iter([(HEAD, False, "OPEN")])

    def read(_repo: str, _number: int) -> tuple[str, bool, str]:
        try:
            return next(reads)
        except StopIteration:
            message = "cannot read"
            raise review_pair.SpecError(message) from None

    monkeypatch.setattr(review_pair, "pull_request_state", read)
    fake(
        monkeypatch,
        {
            "exec a": done(0),
            "wait a": done(0, ANSWER),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\nVERDICT: GO\n"),
        },
    )
    assert review_pair.main([str(pair(tmp_path))], "p") == 1
    assert "head moved to unreadable" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        *(
            (spec, "")
            for spec in [
                "not json",
                [],
                {"runs": [run_spec("a")]},
                {"runs": [run_spec(str(n)) for n in range(4)]},
                {"runs": [run_spec("a"), run_spec("a", harness="agy")]},
                {"runs": [run_spec("a"), "x"]},
                {"runs": [run_spec("a"), run_spec("b", extra=1)]},
                {
                    "runs": [
                        run_spec("a"),
                        {k: v for k, v in run_spec("b").items() if k != "host"},
                    ]
                },
                {"runs": [run_spec("a"), run_spec("b", host="")]},
                {"runs": [run_spec("a"), run_spec("b", fetch_refs="r")]},
                {"runs": [run_spec("a"), run_spec("b", fetch_refs=[1])]},
                {"runs": [run_spec("a"), run_spec("b", head="abc")]},
                {"runs": [run_spec("a"), run_spec("b", harness="agy", repo="x/y")]},
                {"runs": [run_spec("a"), run_spec("b", harness="agy", head="c" * 40)]},
                {"runs": [run_spec("a"), run_spec("b", harness="agy", pr=359)]},
                {"runs": [run_spec("a"), run_spec("b")]},
                {"runs": [run_spec("a"), run_spec("b", harness="agy")], "other": 1},
            ]
        ),
        *(
            (
                {"runs": [run_spec("a"), run_spec("b", pr=pr)]},
                "run 'b': pr is a positive integer",
            )
            for pr in ("358", True, 0)
        ),
        pytest.param(
            {"runs": [run_spec("a"), run_spec("b", harness="code")]},
            "run 'b': harness must be codex, claude or agy",
            id="unknown-harness",
        ),
        (
            {"runs": [run_spec("a"), run_spec("b", harness="claude")]},
            "run 'b': a claude run needs reason and claude_profile",
        ),
        (
            {"runs": [run_spec("a"), run_spec("b", harness="claude", reason="r")]},
            "run 'b': a claude run needs reason and claude_profile",
        ),
    ],
)
def test_a_bad_spec_is_refused_before_any_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spec: object,
    message: str,
) -> None:
    path = tmp_path / "spec.json"
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec))
    monkeypatch.setattr(review_pair, "run_command", pytest.fail)
    assert review_pair.main([str(path)], "p") == 2
    assert capsys.readouterr().err.startswith(f"p: {message}")


def test_a_missing_spec_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert review_pair.main([str(tmp_path / "none.json")], "p") == 2
    assert "cannot read" in capsys.readouterr().err


def test_run_command_captures_output() -> None:
    result = review_pair.run_command(["echo", "hi"])
    assert (result.returncode, result.stdout) == (0, "hi\n")


def test_pull_request_state_reads_one_gh_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    info = {"headRefOid": HEAD, "isDraft": False, "state": "OPEN"}

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return done(0, json.dumps(info))

    monkeypatch.setattr(review_pair.pr, "run", run)
    monkeypatch.setattr(review_pair.pr, "find_tool", tool_name)
    assert READ("tbhb-dev/tools", 358) == (HEAD, False, "OPEN")
    assert seen[0][1:7] == ["pr", "view", "358", "-R", "tbhb-dev/tools", "--json"]


def test_an_unreadable_pull_request_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_pair.pr, "run", fail_read)
    monkeypatch.setattr(review_pair.pr, "find_tool", tool_name)
    with pytest.raises(review_pair.SpecError, match="cannot read pull request 358"):
        READ("tbhb-dev/tools", 358)
