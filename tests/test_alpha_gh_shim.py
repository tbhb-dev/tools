# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for tbhbdev alpha gh-shim and agent-env.

Every test runs with HOME and the XDG directories in tmp_path, no agent
session markers in the environment, and a fake gh that logs its arguments.
The fake answers ``gh api rate_limit`` from its ``core`` file: a JSON value
for ``remaining``, ``fail`` for a valid answer with exit status 1, or
``garbage`` for output that isn't JSON.
"""

import fcntl
import hashlib
import itertools
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import pytest
from fake_tool import pythonpath, write_fake

from tbhbdev import userconfig
from tbhbdev.alpha import gh_shim

SRC = Path(gh_shim.__file__).parents[2]
NOW = 1_000_000.0
FAKE_GH = """import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "gh.log"), "a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["api", "rate_limit"]:
    core_file = os.path.join(here, "core")
    core = open(core_file).read().strip() if os.path.exists(core_file) else "5000"
    if core == "garbage":
        print("not json")
        sys.exit(0)
    remaining = 5000 if core == "fail" else json.loads(core)
    print(json.dumps({"resources": {"core": {"remaining": remaining}}}))
    sys.exit(1 if core == "fail" else 0)
sys.stdout.write("out:" + sys.stdin.read())
sys.stderr.write("err\\n")
sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
"""
AGENT_VARIABLES = (*gh_shim.ROOT_IDS, *gh_shim.MARKERS)
REFUSAL = "gh-shim: refused by the agent gh budget ([gh_budget] in {config}): {why}. "


@dataclass
class World:
    """The fake gh's directory, the shim's directory and the config file."""

    real: Path
    shim: Path
    config: Path

    @property
    def gh(self) -> str:
        """The fake gh."""
        return str(self.real / "gh")

    def calls(self) -> list[list[str]]:
        """The fake gh's argument lists, in order."""
        log = self.real / "gh.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def core(self, value: str) -> None:
        """Set what the fake gh answers for rate_limit."""
        (self.real / "core").write_text(value)

    def budget(self, text: str) -> None:
        """Write the tbhbdev config file."""
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(text)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """HOME and XDG directories in tmp_path, no markers, the fake gh on PATH.

    os.execv raises ExecError, so a test that expects a refusal fails, rather
    than handing the test process to the fake gh, when main execs instead.
    """
    for name in AGENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name, sub in (
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
    ):
        monkeypatch.setenv(name, str(tmp_path / sub))
    real = tmp_path / "real"
    real.mkdir()
    write_fake(real / "gh", FAKE_GH)
    shim = gh_shim.bin_dir()
    shim.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{real}")
    fake_exec(monkeypatch, "execv")
    return World(real, shim, userconfig.path())


def state_file(root: str) -> Path:
    """The budget file of a root session."""
    return gh_shim.state_dir() / f"{hashlib.sha256(root.encode()).hexdigest()}.json"


class ExecError(Exception):
    """Raised by the exec stand-ins in place of replacing the process."""


def fake_exec(monkeypatch: pytest.MonkeyPatch, name: str) -> list[tuple[object, ...]]:
    """Replace os.NAME with a recorder that raises ExecError."""
    seen: list[tuple[object, ...]] = []

    def record(*args: object) -> NoReturn:
        seen.append(args)
        raise ExecError

    monkeypatch.setattr(gh_shim.os, name, record)
    return seen


# ---- paths and the wrapper ----


def test_directories_default_under_home(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    del world
    home = Path(os.environ["HOME"])
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert gh_shim.bin_dir() == home / ".local" / "share" / "tbhbdev" / "agent-bin"
    assert gh_shim.state_dir() == home / ".local" / "state" / "tbhbdev" / "gh-budget"


def test_directories_follow_xdg(world: World, tmp_path: Path) -> None:
    del world
    assert gh_shim.bin_dir() == tmp_path / "data" / "tbhbdev" / "agent-bin"
    assert gh_shim.state_dir() == tmp_path / "state" / "tbhbdev" / "gh-budget"


def test_wrapper_text_runs_this_interpreter_isolated() -> None:
    lines = gh_shim.wrapper_text().split("\n")
    assert lines == [
        "#!/bin/sh",
        "# tbhbdev alpha gh-shim (tbhbdev alpha install)",
        f'exec {shlex.quote(sys.executable)} -I -m tbhbdev alpha gh-shim "$@"',
        "",
    ]


@pytest.mark.parametrize(
    ("path", "rest"),
    [
        ("/a:/b", ["/a", "/b"]),
        ("/a:{shim}:/b:{shim}", ["/a", "/b"]),
        ("/a::/b:", ["/a", "/b"]),
        ("", []),
    ],
)
def test_prefixed_path(world: World, path: str, rest: list[str]) -> None:
    shim = str(world.shim)
    result = gh_shim.prefixed_path(path.format(shim=shim))
    assert result == os.pathsep.join([shim, *rest])


def test_is_shim(world: World, tmp_path: Path) -> None:
    mark = gh_shim.SHIM_MARK
    shim = world.shim / "gh"
    shim.write_text(gh_shim.wrapper_text())
    assert gh_shim.is_shim(str(shim))
    assert not gh_shim.is_shim(world.gh)
    assert not gh_shim.is_shim(str(tmp_path / "missing"))
    assert not gh_shim.is_shim(str(tmp_path))
    edge = tmp_path / "edge"
    edge.write_text("x" * (gh_shim.HEAD - len(mark)) + mark)
    assert gh_shim.is_shim(str(edge))
    edge.write_text("x" * (gh_shim.HEAD - len(mark) + 1) + mark)
    assert not gh_shim.is_shim(str(edge))
    assert gh_shim.HEAD == 512


def test_real_gh_skips_the_shim(world: World, tmp_path: Path) -> None:
    (world.shim / "gh").write_text(gh_shim.wrapper_text())
    (world.shim / "gh").chmod(0o755)
    path = f"{world.shim}{os.pathsep}{os.pathsep}{world.real}"
    assert gh_shim.real_gh({"PATH": path}) == world.gh
    assert gh_shim.real_gh({"PATH": f"{world.shim}{os.pathsep}"}) is None
    assert gh_shim.real_gh({"PATH": str(tmp_path / "nothing")}) is None


def test_real_gh_without_path_searches_the_default(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "defpath", str(world.real))
    assert gh_shim.real_gh({}) == world.gh
    monkeypatch.setattr(os, "defpath", str(world.shim))
    assert gh_shim.real_gh({}) is None


# ---- sessions and config ----


@pytest.mark.parametrize("name", gh_shim.ROOT_IDS)
def test_session_root_names_the_id(name: str) -> None:
    assert gh_shim.session_root({name: "abc", "CLAUDECODE": "1"}) == f"{name}=abc"


def test_session_root_prefers_the_earlier_id() -> None:
    ids = gh_shim.ROOT_IDS
    for first, later in itertools.pairwise(ids):
        env = {later: "later", first: "first"}
        assert gh_shim.session_root(env) == f"{first}=first"
    assert gh_shim.session_root({ids[0]: "", ids[1]: "x"}) == f"{ids[1]}=x"


@pytest.mark.parametrize("name", gh_shim.MARKERS)
def test_session_root_unknown_for_a_marker(name: str) -> None:
    assert gh_shim.session_root({name: "1"}) == "unknown"
    assert gh_shim.session_root({name: ""}) is None


def test_session_root_none_outside_an_agent() -> None:
    assert gh_shim.session_root({"PATH": "/bin"}) is None


def test_markers_are_the_harness_variables() -> None:
    assert gh_shim.ROOT_IDS == (
        "TBHB_GH_ROOT_SESSION",
        "ANTIGRAVITY_CONVERSATION_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
    )
    assert gh_shim.MARKERS == ("CLAUDECODE", "CODEX_HOME", "TBHB_GH_AGENT")


def test_limits_default_without_a_config(world: World) -> None:
    assert not world.config.exists()
    assert gh_shim.limits() == (300, 1000)


def test_limits_default_without_the_table(world: World) -> None:
    world.budget("[other]\nkey = 1\n")
    assert gh_shim.limits() == (300, 1000)


def test_limits_from_the_table(world: World) -> None:
    world.budget("[gh_budget]\nhourly_limit = 0\ncore_floor = 7\n")
    assert gh_shim.limits() == (0, 7)
    world.budget("[gh_budget]\ncore_floor = 7\n")
    assert gh_shim.limits() == (300, 7)


@pytest.mark.parametrize("value", ["-1", "true", '"5"', "1.5"])
@pytest.mark.parametrize("key", ["hourly_limit", "core_floor"])
def test_limits_reject_other_values(world: World, key: str, value: str) -> None:
    world.budget(f"[gh_budget]\n{key} = {value}\n")
    with pytest.raises(userconfig.ConfigError) as caught:
        gh_shim.limits()
    assert str(caught.value) == (
        f"{world.config}: [gh_budget] {key} must be a whole number"
    )


def test_limits_reject_a_bad_config(world: World) -> None:
    world.budget("gh_budget = 3\n")
    with pytest.raises(userconfig.ConfigError, match="must be a table"):
        gh_shim.limits()


# ---- state ----


def test_read_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert gh_shim.read_state(path) == {}
    path.write_text("{")
    assert gh_shim.read_state(path) == {}
    path.write_text("[1]")
    assert gh_shim.read_state(path) == {}
    path.write_text('{"calls": [1]}')
    assert gh_shim.read_state(path) == {"calls": [1]}


def test_recent_calls_keep_the_past_hour(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    calls = [NOW, NOW - 3599.5, NOW - 3600, NOW + 1, "x", None, int(NOW) - 5]
    path.write_text(json.dumps({"calls": calls}))
    result = gh_shim.recent_calls(path, NOW)
    assert result == [NOW, NOW - 3599.5, NOW - 5]
    assert all(type(t) is float for t in result)


def test_recent_calls_without_a_list(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert gh_shim.recent_calls(path, NOW) == []
    path.write_text('{"calls": 3}')
    assert gh_shim.recent_calls(path, NOW) == []


# ---- the core floor ----


def test_sample_reads_core_remaining(world: World) -> None:
    world.core("4321")
    assert gh_shim.sample(world.gh) == 4321
    assert world.calls() == [["api", "rate_limit"]]


@pytest.mark.parametrize("core", ["fail", "garbage", '"12"', "true", "null"])
def test_sample_none_for_a_bad_answer(world: World, core: str) -> None:
    world.core(core)
    assert gh_shim.sample(world.gh) is None


def test_sample_none_without_gh(tmp_path: Path) -> None:
    assert gh_shim.sample(str(tmp_path / "gh")) is None


@pytest.mark.parametrize(
    "stdout", ["[]", '{"resources": {}}', '{"resources": {"core": 3}}']
)
def test_sample_none_for_another_shape(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    seen: list[tuple[object, dict[str, object]]] = []

    def run(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append((args, kwargs))
        return subprocess.CompletedProcess([], 0, stdout, "")

    monkeypatch.setattr(gh_shim.subprocess, "run", run)
    assert gh_shim.sample("/x/gh") is None
    assert seen == [
        (
            ["/x/gh", "api", "rate_limit"],
            {"capture_output": True, "text": True, "check": True, "timeout": 15},
        )
    ]


def test_sample_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(args: list[str], **_: object) -> NoReturn:
        raise subprocess.TimeoutExpired(args, 15)

    monkeypatch.setattr(gh_shim.subprocess, "run", run)
    assert gh_shim.sample("/x/gh") is None


@pytest.mark.parametrize(
    ("cache", "expected"),
    [
        ({"checked": NOW - 59.5, "remaining": 2500}, (False, 2500)),
        ({"checked": NOW, "remaining": 2500}, (False, 2500)),
        ({"checked": NOW - 60, "remaining": 2500}, (True, 2500)),
        ({"checked": NOW + 1, "remaining": 2500}, (True, 2500)),
        ({"checked": "x", "remaining": 2500}, (True, 2500)),
        ({"remaining": 2500}, (True, 2500)),
        ({"checked": NOW, "remaining": None}, (False, None)),
        ({"checked": NOW, "remaining": "5000"}, (False, None)),
        ({"checked": NOW, "remaining": True}, (False, None)),
        ({"checked": NOW}, (False, None)),
        ({}, (True, None)),
    ],
)
def test_core_state(
    tmp_path: Path, cache: dict[str, object], expected: tuple[bool, int | None]
) -> None:
    (tmp_path / "core.json").write_text(json.dumps(cache))
    assert gh_shim.core_state(tmp_path, NOW) == expected


def test_core_state_without_a_cache(tmp_path: Path) -> None:
    assert gh_shim.core_state(tmp_path, NOW) == (True, None)


# ---- reserving a call ----


def test_reserve_counts_a_call(world: World) -> None:
    path = state_file("root")
    gh_shim.reserve(world.gh, "root", NOW, 2, 1000)
    gh_shim.reserve(world.gh, "root", NOW + 1, 2, 1000)
    assert json.loads(path.read_text()) == {"calls": [NOW, NOW + 1]}
    assert world.calls() == [["api", "rate_limit"]]
    cache = gh_shim.state_dir() / "core.json"
    assert json.loads(cache.read_text()) == {"checked": NOW, "remaining": 5000}
    assert path.stat().st_mode & 0o777 == 0o600
    assert cache.stat().st_mode & 0o777 == 0o600
    assert (gh_shim.state_dir() / "lock").exists()
    assert not state_file("other").exists()


def test_reserve_makes_a_private_store(world: World) -> None:
    store = gh_shim.state_dir()
    gh_shim.reserve(world.gh, "root", NOW, 2, 1000)
    assert store.stat().st_mode & 0o777 == 0o700
    assert (store / "lock").stat().st_mode & 0o777 == 0o600


def test_reserve_tightens_an_open_store(world: World) -> None:
    store = gh_shim.state_dir()
    store.mkdir(parents=True)
    store.chmod(0o755)
    (store / "lock").touch(mode=0o644)
    (store / "lock").chmod(0o644)
    gh_shim.reserve(world.gh, "root", NOW, 2, 1000)
    assert store.stat().st_mode & 0o777 == 0o700
    assert (store / "lock").stat().st_mode & 0o777 == 0o600


def test_reserve_refuses_over_the_hourly_limit(world: World) -> None:
    path = state_file("root")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"calls": [NOW - 3600, NOW - 10, NOW - 5]}))
    with pytest.raises(gh_shim.RefusalError) as caught:
        gh_shim.reserve(world.gh, "root", NOW, 2, 1000)
    assert str(caught.value) == (
        "this root session has made 2 gh calls in the past hour, "
        "its budget of 2 per hour"
    )
    assert json.loads(path.read_text()) == {"calls": [NOW - 3600, NOW - 10, NOW - 5]}
    assert world.calls() == []
    assert not (gh_shim.state_dir() / "core.json").exists()


def test_reserve_refuses_over_the_limit_with_a_fresh_cache(world: World) -> None:
    path = state_file("root")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"calls": [NOW - 10]}))
    (path.parent / "core.json").write_text(
        json.dumps({"checked": NOW, "remaining": 5000})
    )
    with pytest.raises(gh_shim.RefusalError, match="made 1 gh calls"):
        gh_shim.reserve(world.gh, "root", NOW, 1, 1000)
    assert world.calls() == []


def test_reserve_refuses_with_a_zero_limit(world: World) -> None:
    with pytest.raises(gh_shim.RefusalError, match="made 0 gh calls"):
        gh_shim.reserve(world.gh, "root", NOW, 0, 1000)
    assert not state_file("root").exists()


def test_reserve_refuses_under_the_core_floor(world: World) -> None:
    world.core("999")
    with pytest.raises(gh_shim.RefusalError) as caught:
        gh_shim.reserve(world.gh, "root", NOW, 300, 1000)
    assert str(caught.value) == (
        "the account has 999 core REST requests left this hour, "
        "under the budget's floor of 1000"
    )
    assert not state_file("root").exists()
    cache = gh_shim.state_dir() / "core.json"
    assert json.loads(cache.read_text()) == {"checked": NOW, "remaining": 999}


def test_reserve_refuses_under_the_floor_from_a_fresh_cache(world: World) -> None:
    store = gh_shim.state_dir()
    store.mkdir(parents=True)
    (store / "core.json").write_text(json.dumps({"checked": NOW, "remaining": 999}))
    with pytest.raises(gh_shim.RefusalError, match="999 core REST requests"):
        gh_shim.reserve(world.gh, "root", NOW + 30, 300, 1000)
    assert world.calls() == []
    assert not state_file("root").exists()


def test_reserve_allows_the_core_floor_itself(world: World) -> None:
    world.core("1000")
    gh_shim.reserve(world.gh, "root", NOW, 300, 1000)
    assert json.loads(state_file("root").read_text()) == {"calls": [NOW]}


@pytest.mark.parametrize("core", ["fail", "garbage"])
def test_reserve_does_not_refuse_for_a_failed_sample(world: World, core: str) -> None:
    world.core(core)
    gh_shim.reserve(world.gh, "root", NOW, 300, 1000)
    cache = gh_shim.state_dir() / "core.json"
    assert json.loads(cache.read_text()) == {"checked": NOW, "remaining": None}
    assert json.loads(state_file("root").read_text()) == {"calls": [NOW]}
    # Within the minute the failure isn't sampled again, and doesn't refuse.
    gh_shim.reserve(world.gh, "root", NOW + 59, 300, 1000)
    assert len(world.calls()) == 1
    assert json.loads(state_file("root").read_text()) == {"calls": [NOW, NOW + 59]}
    # After the minute the sample is tried again, and a good one counts.
    world.core("12")
    with pytest.raises(gh_shim.RefusalError, match="12 core REST requests"):
        gh_shim.reserve(world.gh, "root", NOW + 60, 300, 1000)
    assert len(world.calls()) == 2


def test_reserve_keeps_the_hourly_limit_when_the_sample_fails(world: World) -> None:
    world.core("fail")
    gh_shim.reserve(world.gh, "root", NOW, 1, 1000)
    with pytest.raises(gh_shim.RefusalError, match="made 1 gh calls"):
        gh_shim.reserve(world.gh, "root", NOW + 1, 1, 1000)


def test_reserve_samples_without_holding_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another session's call completes while a sample is running.

    The nested call stands for another process: it takes the lock, finds the
    sample already claimed, and uses the cached answer without sampling.
    """
    store = gh_shim.state_dir()
    store.mkdir(parents=True)
    (store / "core.json").write_text(json.dumps({"checked": 0, "remaining": 4000}))
    samples: list[dict[str, object]] = []

    def sample(gh: str) -> int | None:
        with (store / "lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if held
        samples.append(json.loads((store / "core.json").read_text()))
        gh_shim.reserve(gh, "other", NOW + 1, 300, 1000)
        samples.append(json.loads((store / "core.json").read_text()))
        return 5000

    monkeypatch.setattr(gh_shim, "sample", sample)
    gh_shim.reserve(world.gh, "root", NOW, 300, 1000)
    claimed = {"checked": NOW, "remaining": 4000}
    assert samples == [claimed, claimed]
    cache = json.loads((store / "core.json").read_text())
    assert cache == {"checked": NOW, "remaining": 5000}
    assert json.loads(state_file("root").read_text()) == {"calls": [NOW]}
    assert json.loads(state_file("other").read_text()) == {"calls": [NOW + 1]}


def test_reserve_counts_calls_made_during_the_sample(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is checked again once the sample is back."""

    def sample(gh: str) -> int | None:
        gh_shim.reserve(gh, "root", NOW - 1, 1, 1000)
        return 5000

    monkeypatch.setattr(gh_shim, "sample", sample)
    with pytest.raises(gh_shim.RefusalError, match="made 1 gh calls"):
        gh_shim.reserve(world.gh, "root", NOW, 1, 1000)
    assert json.loads(state_file("root").read_text()) == {"calls": [NOW - 1]}


def test_reserve_checks_the_floor_against_the_new_sample(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim keeps the old answer for others, not for the sampling call."""
    store = gh_shim.state_dir()
    store.mkdir(parents=True)
    (store / "core.json").write_text(json.dumps({"checked": 0, "remaining": 10}))

    def sample(_gh: str) -> int | None:
        return 5000

    monkeypatch.setattr(gh_shim, "sample", sample)
    gh_shim.reserve(world.gh, "root", NOW, 300, 1000)
    assert json.loads(state_file("root").read_text()) == {"calls": [NOW]}


# ---- main and agent-env ----


def test_main_without_gh(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    os.environ["PATH"] = str(world.shim)
    assert gh_shim.main(["pr", "view"], "gh-shim") == 127
    assert capsys.readouterr().err == "gh-shim: no gh on PATH besides the shim\n"


def test_main_passes_through_outside_an_agent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = fake_exec(monkeypatch, "execv")
    with pytest.raises(ExecError):
        gh_shim.main(["pr", "view", "1"], "gh-shim")
    assert seen == [(world.gh, [world.gh, "pr", "view", "1"])]
    assert not gh_shim.state_dir().exists()


def test_main_counts_an_agent_call(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "t1")
    monkeypatch.setattr(gh_shim.time, "time", lambda: NOW)
    seen = fake_exec(monkeypatch, "execv")
    with pytest.raises(ExecError):
        gh_shim.main(["api", "user"], "gh-shim")
    assert seen == [(world.gh, [world.gh, "api", "user"])]
    path = state_file("CODEX_THREAD_ID=t1")
    assert json.loads(path.read_text()) == {"calls": [NOW]}


def test_main_refuses_over_budget(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    world.budget("[gh_budget]\nhourly_limit = 0\n")
    seen = fake_exec(monkeypatch, "execv")
    assert gh_shim.main(["pr", "view"], "gh-shim") == 1
    assert seen == []
    why = (
        "this root session has made 0 gh calls in the past hour, "
        "its budget of 0 per hour"
    )
    assert capsys.readouterr().err == (
        REFUSAL.format(config=world.config, why=why) + gh_shim.POLL_REASON + "\n"
    )


def test_main_refuses_under_the_core_floor(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    world.core("999")
    seen = fake_exec(monkeypatch, "execv")
    assert gh_shim.main(["pr", "view"], "gh-shim") == 1
    assert seen == []
    why = (
        "the account has 999 core REST requests left this hour, "
        "under the budget's floor of 1000"
    )
    assert capsys.readouterr().err == (
        REFUSAL.format(config=world.config, why=why) + gh_shim.POLL_REASON + "\n"
    )


def test_main_refuses_on_a_bad_config(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    world.budget("[gh_budget]\ncore_floor = -1\n")
    assert gh_shim.main(["pr", "view"], "gh-shim") == 1
    assert capsys.readouterr().err == (
        "gh-shim: refused, the agent gh budget can't read or write its config "
        f"or state: {world.config}: [gh_budget] core_floor must be a whole number\n"
    )


def test_main_refuses_when_state_cannot_be_written(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    store = gh_shim.state_dir()
    store.parent.mkdir(parents=True)
    store.write_text("not a directory")
    assert gh_shim.main(["pr", "view"], "gh-shim") == 1
    err = capsys.readouterr().err
    assert err.startswith(
        "gh-shim: refused, the agent gh budget can't read or write its config "
        "or state: [Errno 17] File exists: "
    )
    assert str(store) in err
    assert "awrel" not in err
    assert "poll" not in err.lower()
    assert world.calls() == []


def test_main_does_not_refuse_for_a_failed_sample(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    world.core("fail")
    seen = fake_exec(monkeypatch, "execv")
    with pytest.raises(ExecError):
        gh_shim.main(["pr", "view"], "gh-shim")
    assert seen == [(world.gh, [world.gh, "pr", "view"])]


def test_agent_env_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert gh_shim.agent_env([], "tbhbdev alpha agent-env") == 2
    err = capsys.readouterr().err
    assert err == "usage: tbhbdev alpha agent-env HARNESS [args...]\n"


def test_agent_env_runs_the_harness(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = dict(os.environ)
    seen = fake_exec(monkeypatch, "execvpe")
    with pytest.raises(ExecError):
        gh_shim.agent_env(["codex", "--yolo"], "agent-env")
    [(program, argv, passed)] = seen
    assert (program, argv) == ("codex", ["codex", "--yolo"])
    env = cast("dict[str, str]", passed)
    root = env.pop("TBHB_GH_ROOT_SESSION")
    assert len(root) == 32
    assert int(root, 16) >= 0
    assert env == {
        **before,
        "PATH": f"{world.shim}{os.pathsep}{world.real}",
        "TBHB_GH_AGENT": "1",
    }
    assert dict(os.environ) == before


def test_agent_env_without_path_starts_from_the_default(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PATH")
    monkeypatch.setattr(os, "defpath", str(world.real))
    seen = fake_exec(monkeypatch, "execvpe")
    with pytest.raises(ExecError):
        gh_shim.agent_env(["agy"], "agent-env")
    env = seen[0][2]
    assert isinstance(env, dict)
    assert env["PATH"] == f"{world.shim}{os.pathsep}{world.real}"


def test_agent_env_keeps_an_outer_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TBHB_GH_ROOT_SESSION", "outer")
    seen = fake_exec(monkeypatch, "execvpe")
    with pytest.raises(ExecError):
        gh_shim.agent_env(["agy"], "agent-env")
    env = seen[0][2]
    assert isinstance(env, dict)
    assert env["TBHB_GH_ROOT_SESSION"] == "outer"


def test_agent_env_reports_a_missing_harness(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    del world
    assert gh_shim.agent_env(["no-such-harness"], "agent-env") == 127
    err = capsys.readouterr().err
    assert err.startswith("agent-env: cannot run no-such-harness: ")


# ---- through a real process tree ----


def install_test_shim(world: World) -> None:
    """A shim that runs this checkout's tbhbdev, with the shim's marker.

    The installed wrapper runs ``python -I``, which ignores PYTHONPATH, and
    under Bazel the package comes only through PYTHONPATH.
    """
    python = shlex.quote(sys.executable)
    source = shlex.quote(pythonpath(SRC))
    shim = world.shim / "gh"
    shim.write_text(
        f"#!/bin/sh\n{gh_shim.SHIM_MARK}\n"
        f'PYTHONPATH={source} exec {python} -m tbhbdev alpha gh-shim "$@"\n'
    )
    shim.chmod(0o755)


def run_child(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """A Python child that runs ``gh ARGS`` from PATH, as a script would."""
    code = "import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:]).returncode)"
    return subprocess.run(
        [sys.executable, "-c", code, "gh", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        input="in",
    )


def test_a_child_passes_through_outside_an_agent(world: World) -> None:
    install_test_shim(world)
    env = {**os.environ, "FAKE_GH_EXIT": "3"}
    done = run_child(env, "pr", "view", "a b", "")
    assert (done.returncode, done.stdout, done.stderr) == (3, "out:in", "err\n")
    assert world.calls() == [["pr", "view", "a b", ""]]
    assert not gh_shim.state_dir().exists()


@pytest.mark.parametrize(
    "marker",
    [
        "CLAUDECODE",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_HOME",
        "ANTIGRAVITY_CONVERSATION_ID",
    ],
)
def test_a_child_inherits_each_marker(world: World, marker: str) -> None:
    install_test_shim(world)
    world.budget("[gh_budget]\nhourly_limit = 1\n")
    env = {**os.environ, marker: "m1"}
    first = run_child(env, "pr", "view")
    assert (first.returncode, first.stdout) == (0, "out:in")
    second = run_child(env, "pr", "view")
    assert second.returncode == 1
    assert second.stdout == ""
    assert "budget of 1 per hour" in second.stderr
    assert "Subscribe to the awrel event instead" in second.stderr
    assert world.calls() == [["api", "rate_limit"], ["pr", "view"]]
    root = "unknown" if marker in gh_shim.MARKERS else f"{marker}=m1"
    assert len(json.loads(state_file(root).read_text())["calls"]) == 1
