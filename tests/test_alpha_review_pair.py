"""Tests for `review pair`."""

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import review, review_pair

if TYPE_CHECKING:
    from pathlib import Path

HEAD = "a" * 40


def run_spec(name: str, **extra: object) -> dict[str, object]:
    return {
        "name": name,
        "prompt_file": f"/p/{name}.md",
        "host": "review-host",
        "repo": "tbhb-dev/tools",
        "model": "gpt-6.2-sol",
        "effort": "high",
        "head": HEAD,
        **extra,
    }


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    return path


def pair(tmp_path: Path) -> Path:
    return write(tmp_path, {"runs": [run_spec("a"), run_spec("b")]})


def done(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def fake(
    monkeypatch: pytest.MonkeyPatch, replies: dict[str, object]
) -> list[list[str]]:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        verb = argv[len(review_pair.LAUNCHER)]
        reply = replies[f"{verb} {argv[-1] if verb == 'wait' else argv[-2]}"]
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
    assert "  findings: P1=0 P2=2 P3=1\n  log: tbhbdev alpha agent log a\n" in out
    assert "  verdict: RECOMMEND HOLD** P1 (1) z\n  findings: P1=1" in out
    first = next(argv for argv in seen if argv[-1] == "/p/a.md")
    assert first[len(review_pair.LAUNCHER) :] == [
        "exec", "--profile", "review", "--host", "review-host",
        "--repo", "tbhb-dev/tools", "--model", "gpt-6.2-sol",
        "--effort", "high", "a", "/p/a.md",
    ]  # fmt: skip


def test_optional_fields_become_flags() -> None:
    run = run_spec(
        "a", harness="codex", account="x", claude_profile="y", fetch_refs=["r1", "r2"]
    )
    argv = review_pair.exec_argv(run)
    assert argv[-6:] == ["--fetch-ref", "r1", "--fetch-ref", "r2", "a", "/p/a.md"]
    assert argv.count("--fetch-ref") == 2
    assert argv[argv.index("--harness") + 1] == "codex"
    assert argv[argv.index("--account") + 1] == "x"


def test_a_run_that_fails_to_launch_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake(
        monkeypatch,
        {
            "exec a": done(1, "x\n", "launch refused\n"),
            "exec b": done(0),
            "wait b": done(0, "b: rc=0\nVERDICT: GO\n"),
        },
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 1
    out = capsys.readouterr().out
    assert "state: not launched (launch refused)" in out
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
    assert "state: rc=unknown (boom)" in out


def test_empty_output_has_a_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake(
        monkeypatch,
        {"exec a": done(2), "exec b": done(2)},
    )
    assert review.main(["pair", str(pair(tmp_path))], "p") == 1
    assert "state: not launched (no output)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "spec",
    [
        "not json",
        [],
        {"runs": [run_spec("a")]},
        {"runs": [run_spec(str(n)) for n in range(5)]},
        {"runs": [run_spec("a"), run_spec("a")]},
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
        {"runs": [run_spec("a"), run_spec("b")], "other": 1},
    ],
)
def test_a_bad_spec_is_refused_before_any_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spec: object,
) -> None:
    path = tmp_path / "spec.json"
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec))
    monkeypatch.setattr(review_pair, "run_command", pytest.fail)
    assert review_pair.main([str(path)], "p") == 2
    assert capsys.readouterr().err.startswith("p: ")


def test_a_missing_spec_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert review_pair.main([str(tmp_path / "none.json")], "p") == 2
    assert "cannot read" in capsys.readouterr().err


def test_run_command_captures_output() -> None:
    result = review_pair.run_command(["echo", "hi"])
    assert (result.returncode, result.stdout) == (0, "hi\n")
