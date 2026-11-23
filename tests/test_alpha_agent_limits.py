"""Launch limits: pure decisions, the remote script and the launch."""

from __future__ import annotations

import copy
import dataclasses
import json
import subprocess
import sys
import time
from random import Random
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import Remote, setup
from agent_example import LINUX, MARKER, settings
from test_alpha_agent_run_host import make_run

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, placement, run_host, runfiles, taint_store
from tbhbdev.alpha.agent.decisions import limits, remote, taint
from tbhbdev.alpha.agent.model import Host, Invalid, RunRequest, Settings

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107

CONFIG = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")
FINE = limits.Reading(1.0, 8, 1000, 500, 100, 10, 90, 90, 0)


def test_parse_reads_host_numbers() -> None:
    assert limits.parse("0.5 4 8388608 7340032 100 0 90 90 0\n") == limits.Reading(
        0.5, 4, 8388608, 7340032, 100, 0, 90, 90, 0
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1 2 3",
        "x 4 1000 900 100 0 90 90 0",
        "1 4 1000 900 100 0 7 90 90 0",
        "nan 4 1000 900 100 0 90 90 0",
        "-1 4 1000 900 100 0 90 90 0",
        "1 0 1000 900 100 0 90 90 0",
        "1 4 0 0 100 0 90 90 0",
        "1 4 1000 -1 100 0 90 90 0",
        "1 4 1000 900 -1 0 90 90 0",
        "1 4 1000 900 100 -1 90 90 0",
        "1 4 1000 900 0 1 90 90 0",
        "1 4 1000 900 0 0 101 90 0",
        "1 4 1000 900 0 0 90 -1 0",
        "1 4 1000 900 0 0 90 90 -1",
    ],
)
def test_parse_refuses_anything_else(text: str) -> None:
    assert limits.parse(text) == "unparsable output"


def test_decide_passes_a_quiet_host() -> None:
    assert limits.decide(LINUX, FINE, ignore=False) == (False, "")


def test_decide_names_every_number_over_a_limit() -> None:
    busy = limits.Reading(8.0, 8, 1000, 100, 100, 90, 90, 90, 0)
    refuse, message = limits.decide(LINUX, busy, ignore=False)
    assert refuse
    assert message == (
        "example-linux: load 8 >= limit 8; memory available 10% < 20%; "
        "swap used 90% > 80%; pass --ignore-limits to launch anyway"
    )


def test_decide_uses_the_configured_limits() -> None:
    host = dataclasses.replace(
        LINUX, load_limit=24, min_memory_available_percent=10, max_swap_used_percent=95
    )
    reading = limits.Reading(23.9, 32, 1000, 100, 100, 90, 90, 90, 0)
    assert limits.decide(host, reading, ignore=False) == (False, "")
    low = dataclasses.replace(host, load_limit=2)
    assert limits.decide(low, reading, ignore=False)[0]


def test_decide_allows_swap_of_zero() -> None:
    reading = limits.Reading(1.0, 8, 1000, 500, 0, 0, 90, 90, 0)
    assert limits.decide(LINUX, reading, ignore=False) == (False, "")


def test_decide_refuses_a_failed_read() -> None:
    refuse, message = limits.decide(LINUX, "cause", ignore=False)
    assert refuse
    assert message == (
        "example-linux: cannot read host limits: cause; "
        "pass --ignore-limits to launch anyway"
    )


@pytest.mark.parametrize("found", ["cause", dataclasses.replace(FINE, load=9)])
def test_decide_warns_when_ignored_or_warn_only(found: limits.Reading | str) -> None:
    warn_only = dataclasses.replace(LINUX, limits_warn_only=True)
    for refuse, message in (
        limits.decide(LINUX, found, ignore=True),
        limits.decide(warn_only, found, ignore=False),
    ):
        assert not refuse
        assert message.startswith("warning: example-linux: ")


def test_script_prints_the_numbers_of_this_host() -> None:
    script = remote.limits()
    assert script.startswith("# tbhbdev-limits\n")
    done = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=True
    )
    assert isinstance(limits.parse(done.stdout), limits.Reading)


@pytest.mark.parametrize(
    "key", ["vm.loadavg", "hw.logicalcpu", "hw.memsize", "vm.swapusage"]
)
def test_a_failed_macos_read_ends_the_script_nonzero(key: str) -> None:
    script = remote.limits()
    lines = [x for x in script.splitlines() if f"sysctl -n {key})" in x]
    assert len(lines) == 1
    assert lines[0].endswith("|| exit 1")
    if sys.platform == "darwin":
        broken = script.replace(f"sysctl -n {key}", "false")
        done = subprocess.run(
            ["/bin/sh", "-c", broken], capture_output=True, text=True, check=False
        )
        assert (done.returncode, done.stdout) == (1, "")


def run_script(script: str) -> tuple[int, str]:
    done = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=False
    )
    return done.returncode, done.stdout


@pytest.mark.parametrize(
    "text", ["read l _ < /proc/loadavg", "c=$(nproc)", "v=$(vm_stat)"]
)
def test_every_other_read_ends_the_script_nonzero_too(text: str) -> None:
    lines = [x for x in remote.limits().splitlines() if text in x]
    assert len(lines) == 1
    assert lines[0].endswith("|| exit 1")


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/proc/loadavg", "/nonexistent"),
        ("$(nproc)", "$(false)"),
    ],
)
def test_a_failed_linux_read_ends_the_script_nonzero(old: str, new: str) -> None:
    script = remote.limits().replace(old, new)
    assert run_script(script) == (1, "")


MEMINFO = {
    "MemTotal": "10 kB",
    "MemAvailable": "5 kB",
    "SwapTotal": "0 kB",
    "SwapFree": "0 kB",
}


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
@pytest.mark.parametrize("field", list(MEMINFO))
@pytest.mark.parametrize("damage", ["missing", "empty", "word"])
def test_each_linux_memory_field_must_be_a_number(
    field: str, damage: str, tmp_path: Path
) -> None:
    lines = {
        name: {"missing": None, "empty": f"{name}:", "word": f"{name}: x kB"}[damage]
        if name == field
        else f"{name}: {value}"
        for name, value in MEMINFO.items()
    }
    (tmp_path / "meminfo").write_text(
        "".join(f"{line}\n" for line in lines.values() if line)
    )
    script = remote.limits().replace("/proc/meminfo", str(tmp_path / "meminfo"))
    assert run_script(script) == (1, "")


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_a_swapless_linux_host_with_zero_fields_is_readable(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(
        "".join(f"{name}: {value}\n" for name, value in MEMINFO.items())
    )
    script = remote.limits().replace("/proc/meminfo", str(tmp_path / "meminfo"))
    code, out = run_script(script)
    assert code == 0
    assert isinstance(limits.parse(out), limits.Reading)


@pytest.mark.skipif(sys.platform != "darwin", reason="reads sysctl and vm_stat")
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("$(vm_stat)", "$(echo 'Pages free: 5.'; false)"),
        (
            "$(vm_stat)",
            "$(echo 'Mach Virtual Memory Statistics: (page size of 16384 bytes)')",
        ),
        ("sysctl -n vm.swapusage", "echo 'total =  used = 5.00M  free = 1.00M'"),
        ("sysctl -n vm.swapusage", "echo 'garbage'"),
    ],
)
def test_a_partial_macos_read_ends_the_script_nonzero(old: str, new: str) -> None:
    assert run_script(remote.limits().replace(old, new)) == (1, "")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    taint_store.record_clearance(
        taint.Clearance("example-linux", "Test host rebuilt", "2026-11-10T00:00:00Z")
    )

    def configured() -> Settings:
        return CONFIG

    monkeypatch.setattr(agent.settings, "load", configured)
    return result


def launch(host: Remote, *options: str) -> int:
    args = [
        "exec", "--host", "example-linux", "--repo", "example-org/example-repo",
        "--model", "m", "--effort", "high", "--profile", "build", *options,
        "sample", str(host.prompt),
    ]  # fmt: skip
    return agent.main(args, "example agent")


def test_a_busy_host_refuses_before_any_transfer(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits="9 4 1000 900 100 0 90 90 0")
    assert launch(host) == 1
    assert "load 9 >= limit 4" in capsys.readouterr().err
    assert len(host.records("ssh", limits=True)) == 1
    assert host.records("ssh") == []
    assert not driver.metadata_path("sample").exists()


def test_ignore_limits_launches_warns_and_is_recorded(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits="9 4 1000 900 100 0 90 90 0")
    assert launch(host, "--ignore-limits") == 0
    assert "warning: example-linux: load 9 >= limit 4" in capsys.readouterr().err
    assert json.loads(driver.metadata_path("sample").read_text())["ignore_limits"]
    assert driver.load(CONFIG, "sample").ignore_limits


def test_a_failed_read_refuses(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits_rc=1)
    assert launch(host) == 1
    assert "cannot read host limits: example-linux: remote command" in (
        capsys.readouterr().err
    )
    host.configure(limits_rc=0, limits="garbage")
    assert launch(host) == 1
    assert "cannot read host limits: unparsable output" in capsys.readouterr().err


def test_a_quiet_host_launches_without_a_warning(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(host) == 0
    assert "warning" not in capsys.readouterr().err
    assert not json.loads(driver.metadata_path("sample").read_text())["ignore_limits"]


@pytest.mark.parametrize(("cpus", "jobs"), [(1, 1), (17, 5)])
def test_bazel_share(tmp_path: Path, cpus: int, jobs: int) -> None:
    found = dataclasses.replace(FINE, cpus=cpus, memory_total=12289 * 1024)
    caps = limits.bazel_caps(dataclasses.replace(LINUX, max_runs_per_host=3), found)
    assert caps == (jobs, 4096)
    value = make_run(tmp_path)
    run = dataclasses.replace(
        value, request=dataclasses.replace(value.request, bazel=caps)
    )
    run.tmpdir.mkdir(exist_ok=True)
    assert run_host.bazel_env(run) == {"BAZELRC": str(run.tmpdir / "bazelrc")}
    options = f"--jobs={jobs} --local_resources=memory=4096"
    assert (run.tmpdir / "bazelrc").read_text() == f"build {options}\n"
    assert options in run.files.path("log").read_text()


def test_auto_records_choice_and_reasons(
    host: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read(
        _cfg: Settings, target: Host, repository: str | None, timeout: float
    ) -> limits.Reading:
        assert repository == "example-org/example-repo"
        assert timeout > 0
        return (
            dataclasses.replace(FINE, disk_free=5, inode_free=3)
            if target != LINUX
            else FINE
        )

    monkeypatch.setattr(placement, "probe", read)
    assert launch(host, "--host", "auto") == 0
    run = driver.load(CONFIG, "sample")
    assert run.host == LINUX
    assert run.placement == {
        CONFIG.hosts[0].name: "eligible, headroom 30, running 0",
        CONFIG.hosts[1].name: "disk free 5% < 10%; inodes free 3% < 10%",
    }
    assert run.request.bazel == (4, 1)


@pytest.mark.parametrize(
    "case",
    [
        (dataclasses.replace(LINUX, proven_harnesses=()), "harness"),
        (dataclasses.replace(LINUX, models=("other",)), "model"),
        (dataclasses.replace(LINUX, accounts=LINUX.accounts[1:]), "account"),
        (LINUX, "mirror"),
        (dataclasses.replace(LINUX, limits_warn_only=True), "cap 2"),
    ],
)
def test_auto_refuses_each_ineligible_host(
    host: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[Host, str],
) -> None:
    target, detail = case

    def read(
        _cfg: Settings, _host: Host, _repository: str | None, _timeout: float
    ) -> limits.Reading:
        return dataclasses.replace(FINE, running=2)

    def configured() -> Settings:
        return dataclasses.replace(CONFIG, hosts=(target,))

    if detail == "mirror":
        host.configure(limits="repository mirror missing")
    else:
        monkeypatch.setattr(placement, "probe", read)
    monkeypatch.setattr(agent.settings, "load", configured)
    assert (
        launch(host, "--host", "auto", "--account", LINUX.account, "--ignore-limits")
        == 1
    )
    error = capsys.readouterr().err
    assert f"no host has headroom\n{LINUX.name}: " in error
    assert detail in error.splitlines()[-1]
    assert not driver.metadata_path("sample").exists()


@pytest.mark.parametrize("prefer_one", [False, True])
def test_placement_ranks_by_headroom_and_spreads_ties(
    host: Remote, monkeypatch: pytest.MonkeyPatch, prefer_one: bool
) -> None:
    def read(
        _cfg: Settings, target: Host, _repository: str | None, _timeout: float
    ) -> limits.Reading:
        if prefer_one and target != LINUX:
            return dataclasses.replace(FINE, memory_available=300)
        return FINE

    monkeypatch.setattr(placement, "probe", read)
    assert launch(host) == 0
    run: RunRequest = driver.load(CONFIG, "sample").request
    chosen = {
        placement.select(
            CONFIG,
            run,
            None,
            time.monotonic() + 10,
            rng=Random(seed),  # noqa: S311 -- deterministic placement
        )[0].name
        for seed in range(8)
    }
    assert chosen == ({LINUX.name} if prefer_one else {h.name for h in CONFIG.hosts})


def test_concurrent_auto_fails_without_waiting(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    with runfiles.locked(runfiles.private_dir(driver.directory()) / "placement.lock"):
        assert launch(host, "--host", "auto") == 1
    assert "another automatic placement is in progress" in capsys.readouterr().err


def test_check_limits_runs_without_a_launch(host: Remote) -> None:
    host.configure(limits="9 4 1000 900 100 0 90 90 0")
    config_host = CONFIG.hosts[0]
    with pytest.raises(Invalid, match="load 9"):
        driver.check_limits(CONFIG, config_host, ignore=False)
    driver.check_limits(CONFIG, config_host, ignore=True)


def test_load_accepts_a_record_from_before_the_field(host: Remote) -> None:
    assert launch(host) == 0
    path = driver.metadata_path("sample")
    record = json.loads(path.read_text())
    del record["ignore_limits"]
    path.write_text(json.dumps(copy.deepcopy(record)) + "\n")
    assert driver.load(CONFIG, "sample").ignore_limits is False


def test_warn_only_keeps_launching_when_the_probe_fails(host: Remote) -> None:
    host.configure(limits_rc=1)
    assert launch(host, "--ignore-limits") == 0
    assert driver.load(CONFIG, "sample").request.bazel is None
