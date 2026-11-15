"""Tests for tbhbdev alpha version-check, with a fake runner and no ssh."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import version_check

A = "a" * 40
B = "b" * 40
C = "c" * 40
PROG = "tbhbdev alpha version-check"


def line(stamp: str) -> str:
    return f"tbhbdev 0 ({stamp})\n"


class Fake:
    """A runner that answers by host (the argv's second to last item)."""

    def __init__(self, answers: dict[str, tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[0] == "git":
            return self.answers["git"]
        return self.answers[argv[-2]]


@pytest.fixture(autouse=True)
def _here(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_check, "local_name", lambda: "here")


def ssh(host: str) -> list[str]:
    return [
        "ssh",
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        host,
        "$HOME/.local/bin/tbhbdev --version",
    ]


def run_main(
    argv: list[str], fake: Fake, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    status = version_check.main(argv, PROG, fake)
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_all_hosts_equal_exit_zero_and_ssh_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"h1": (0, line(A)), "h2": (0, line(A))})
    status, out, err = run_main(["--host", "h1", "--host", "h2"], fake, capsys)
    assert (status, out, err) == (0, f"h1 {A} ok\nh2 {A} ok\n", "")
    assert fake.calls == [ssh("h1"), ssh("h2")]


def test_a_disagreeing_host_is_stale_against_the_majority(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"h1": (0, line(B)), "h2": (0, line(A)), "h3": (0, line(A))})
    status, out, _ = run_main(
        ["--host", "h1", "--host", "h2", "--host", "h3"], fake, capsys
    )
    assert status == 1
    assert out == f"h1 {B} stale\nh2 {A} ok\nh3 {A} ok\n"


def test_a_tie_takes_the_first_host_as_the_reference(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"h1": (0, line(B)), "h2": (0, line(A))})
    status, out, _ = run_main(["--host", "h1", "--host", "h2"], fake, capsys)
    assert (status, out) == (1, f"h1 {B} ok\nh2 {A} stale\n")


def test_expect_sha_compares_every_host_to_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"h1": (0, line(A)), "h2": (0, line(A))})
    status, out, _ = run_main(
        ["--host", "h1", "--host", "h2", "--expect", B], fake, capsys
    )
    assert (status, out) == (1, f"h1 {A} stale\nh2 {A} stale\n")


def test_expect_latest_reads_main_once(capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({"git": (0, f"{B}\trefs/heads/main\n"), "h1": (0, line(B))})
    status, out, _ = run_main(["--host", "h1", "--expect", "latest"], fake, capsys)
    assert (status, out) == (0, f"h1 {B} ok\n")
    assert fake.calls[0] == [
        "git",
        "ls-remote",
        "https://github.com/tbhb-dev/tools",
        "refs/heads/main",
    ]
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "answer", [(1, f"{B}\trefs/heads/main\n"), (0, ""), (0, f"{B}\trefs/tags/x\n")]
)
def test_expect_latest_unreadable_exits_three(
    answer: tuple[int, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake({"git": answer})
    status, out, err = run_main(["--host", "h1", "--expect", "latest"], fake, capsys)
    assert (status, out) == (3, "")
    assert err == (f"{PROG}: cannot read main of https://github.com/tbhb-dev/tools\n")
    assert len(fake.calls) == 1


def test_unreachable_alone_exits_three(capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({"h1": (0, line(A)), "h2": (255, "")})
    status, out, _ = run_main(["--host", "h1", "--host", "h2"], fake, capsys)
    assert (status, out) == (3, f"h1 {A} ok\nh2 unknown unreachable\n")


def test_stale_wins_over_unreachable(capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake({"h1": (0, line(B)), "h2": (255, "")})
    status, out, _ = run_main(
        ["--host", "h1", "--host", "h2", "--expect", A], fake, capsys
    )
    assert (status, out) == (1, f"h1 {B} stale\nh2 unknown unreachable\n")


@pytest.mark.parametrize(
    "answer",
    [
        (127, ""),
        (1, line(A)),
        (0, ""),
        (0, "tbhbdev 0 (editable /src/tools)\n"),
        (0, "tbhbdev 0 (unknown)\n"),
        (0, f"tbhbdev 0 ({A[:39]})\n"),
        (0, f"tbhbdev 0 ({A}0)\n"),
    ],
)
def test_an_unparsable_or_failed_answer_is_unknown(
    answer: tuple[int, str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake({"h1": answer})
    status, out, _ = run_main(["--host", "h1", "--expect", A], fake, capsys)
    assert (status, out) == (1, "h1 unknown unknown\n")


def test_all_unknown_without_expect_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"h1": (0, "x")})
    status, out, _ = run_main(["--host", "h1"], fake, capsys)
    assert (status, out) == (1, "h1 unknown unknown\n")


def test_the_version_line_may_follow_other_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"h1": (0, f"motd\n{line(A)}")})
    status, out, _ = run_main(["--host", "h1", "--expect", A], fake, capsys)
    assert (status, out) == (0, f"h1 {A} ok\n")


def test_the_local_host_runs_the_command_without_ssh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / ".local" / "bin" / "tbhbdev"
    path.parent.mkdir(parents=True)
    path.touch()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def runner(argv: list[str]) -> tuple[int, str]:
        assert argv == [str(path), "--version"]
        return 0, line(A)

    status = version_check.main(["--host", "HERE"], PROG, runner)
    assert (status, capsys.readouterr().out) == (0, f"HERE {A} ok\n")


def test_a_missing_local_binary_is_unknown_not_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def runner(argv: list[str]) -> tuple[int, str]:
        raise AssertionError(argv)

    status = version_check.main(["--host", "here"], PROG, runner)
    assert (status, capsys.readouterr().out) == (1, "here unknown unknown\n")


def test_json_lists_host_version_and_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"h1": (0, line(A)), "h2": (255, ""), "h3": (0, line(B))})
    status, out, _ = run_main(
        ["--json", "--host", "h1", "--host", "h2", "--host", "h3", "--expect", A],
        fake,
        capsys,
    )
    assert status == 1
    assert (
        out
        == json.dumps(
            [
                {"host": "h1", "version": A, "state": "ok"},
                {"host": "h2", "version": "unknown", "state": "unreachable"},
                {"host": "h3", "version": B, "state": "stale"},
            ]
        )
        + "\n"
    )


@pytest.mark.usefixtures("config_file")
def test_no_configured_hosts_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, out, err = run_main([], Fake({}), capsys)
    assert (status, out) == (2, "")
    assert err == f"{PROG}: no hosts: pass --host or set [harness] hosts\n"


def test_default_hosts_come_from_the_user_config(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text('[harness]\nhosts = ["x1=Mac", "x2"]\n')
    fake = Fake({"x1": (0, line(A)), "x2": (0, line(A))})
    status, out, _ = run_main([], fake, capsys)
    assert (status, out) == (0, f"x1 {A} ok\nx2 {A} ok\n")


def test_a_broken_config_is_a_usage_error(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text("[harness]\nhosts = 3\n")
    status, out, err = run_main([], Fake({}), capsys)
    assert (status, out) == (2, "")
    assert err.startswith(f"{PROG}: ")
    assert err.endswith("\n")
    assert "None" not in err
    assert "hosts" in err


@pytest.mark.parametrize(
    "name",
    [
        "-oProxyCommand=x",
        "a b",
        "",
        "h;x",
        ".h",
        "@h",
        "u@",
        "u@@h",
        "-u@h",
        "u@-h",
        "a@b@c",
        pytest.param("-h", id="a leading dash would pass as an ssh option"),
        pytest.param("h_", id="a host takes no underscore"),
        pytest.param("h:22", id="a host takes no port"),
        pytest.param("h/x", id="a host takes no path"),
        pytest.param("a b@h", id="a login takes no space"),
    ],
)
def test_a_bad_host_name_is_a_usage_error(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake({})
    status, out, err = run_main(["--host", "h1", f"--host={name}"], fake, capsys)
    assert (status, out) == (2, "")
    assert err == f"{PROG}: not a host name: {name!r}\n"
    assert fake.calls == []


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a", id="a one-character host"),
        pytest.param("a.b-c", id="a host holds dots and dashes"),
        pytest.param("agent@a", id="a login before a one-character host"),
        pytest.param("ag.ent-1@h", id="a login holds dots, dashes and digits"),
        pytest.param("a_b@h", id="a login holds an underscore"),
        pytest.param("u@host-name", id="a dashed host after a login"),
    ],
)
def test_a_valid_host_name_is_checked(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake({name: (0, line(A))})
    status, out, err = run_main(["--host", name], fake, capsys)
    assert (status, out, err) == (0, f"{name} {A} ok\n", "")


@pytest.mark.parametrize("expect", ["", "abc", A.upper(), A + "0", "Latest"])
def test_a_bad_expect_is_a_usage_error(
    expect: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake({})
    status, out, err = run_main(["--host", "h1", "--expect", expect], fake, capsys)
    assert (status, out) == (2, "")
    assert err == f"{PROG}: --expect wants 40 hex digits or latest: {expect!r}\n"
    assert fake.calls == []


def test_an_unknown_option_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, err = run_main(["--bogus"], Fake({}), capsys)
    assert (status, out) == (2, "")
    assert "unrecognized arguments: --bogus" in err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    status, out, _ = run_main(["--help"], Fake({}), capsys)
    assert status == 0
    assert out.startswith(f"usage: {PROG}")
    assert "[--host NAME]" in out
    assert "[--expect SHA|latest]" in out
    assert "compare the installed build on each host." in out


def test_run_returns_status_and_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 4, stdout="out", stderr="e")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert version_check.run(["x", "y"]) == (4, "out")
    assert seen == {
        "argv": ["x", "y"],
        "kwargs": {
            "capture_output": True,
            "text": True,
            "timeout": 30,
            "check": False,
        },
    }


@pytest.mark.parametrize(
    "error", [FileNotFoundError("x"), subprocess.TimeoutExpired("x", 30)]
)
def test_run_reports_255_when_the_command_cannot_run(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert version_check.run(["x"]) == (255, "")


def test_local_name_is_the_short_lowercase_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr("socket.gethostname", lambda: "Host-A.example.local")
    assert version_check.local_name() == "host-a"


@pytest.mark.parametrize(
    ("states", "status"),
    [
        ([], 0),
        (["ok", "ok"], 0),
        (["ok", "stale"], 1),
        (["unknown"], 1),
        (["unreachable", "ok"], 3),
        (["unreachable", "stale"], 1),
        (["unreachable", "unknown"], 1),
    ],
)
def test_exit_status(states: list[str], status: int) -> None:
    assert version_check.exit_status(states) == status


def test_reference_without_stamps_is_none() -> None:
    assert version_check.reference([None, None]) is None
    assert version_check.reference([]) is None


def test_the_alpha_dispatcher_runs_version_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], str]] = []

    def main(argv: list[str], prog: str) -> int:
        seen.append((argv, prog))
        return 7

    monkeypatch.setitem(alpha.COMMANDS, "version-check", main)
    assert alpha.main(["version-check", "--json"], "tbhbdev alpha") == 7
    assert seen == [(["--json"], "tbhbdev alpha version-check")]


def test_the_alpha_usage_lists_version_check() -> None:
    assert "version-check [--host NAME ...]" in alpha.USAGE
    assert alpha.COMMANDS["version-check"] is version_check.main


def test_a_user_at_host_is_the_ssh_target_and_the_reported_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"agent@lab1": (0, line(A))})
    status, out, err = run_main(["--host", "agent@lab1"], fake, capsys)
    assert (status, out, err) == (0, f"agent@lab1 {A} ok\n", "")
    assert fake.calls == [ssh("agent@lab1")]


def test_a_user_at_this_host_still_goes_over_ssh(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = Fake({"agent@here": (0, line(A))})
    status, out, _ = run_main(["--host", "agent@here"], fake, capsys)
    assert (status, out) == (0, f"agent@here {A} ok\n")
    assert fake.calls == [ssh("agent@here")]
