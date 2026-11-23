"""Launch limits: pure decisions, the remote script and the launch."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import Remote, setup
from agent_example import LINUX, MARKER, settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, ownership, runfiles, taint_store
from tbhbdev.alpha.agent.decisions import limits, remote, taint
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107

CONFIG = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")
FINE = limits.Reading(1.0, 8, 1000, 500, 100, 10)


def test_parse_reads_six_numbers() -> None:
    assert limits.parse("0.5 4 1000 900 100 0\n") == limits.Reading(
        0.5, 4, 1000, 900, 100, 0
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1 2 3",
        "x 4 1000 900 100 0",
        "1 4 1000 900 100 0 7",
        "nan 4 1000 900 100 0",
        "-1 4 1000 900 100 0",
        "1 0 1000 900 100 0",
        "1 4 0 0 100 0",
        "1 4 1000 -1 100 0",
        "1 4 1000 900 -1 0",
        "1 4 1000 900 100 -1",
    ],
)
def test_parse_refuses_anything_else(text: str) -> None:
    assert limits.parse(text) == "unparsable output"


def test_decide_passes_a_quiet_host() -> None:
    assert limits.decide(LINUX, FINE, ignore=False) == (False, "")


def test_decide_names_every_number_over_a_limit() -> None:
    busy = limits.Reading(8.0, 8, 1000, 100, 100, 90)
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
    reading = limits.Reading(23.9, 32, 1000, 100, 100, 90)
    assert limits.decide(host, reading, ignore=False) == (False, "")
    low = dataclasses.replace(host, load_limit=2)
    assert limits.decide(low, reading, ignore=False)[0]


def test_decide_allows_swap_of_zero() -> None:
    reading = limits.Reading(1.0, 8, 1000, 500, 0, 0)
    assert limits.decide(LINUX, reading, ignore=False) == (False, "")


def test_decide_refuses_a_failed_read() -> None:
    refuse, message = limits.decide(LINUX, "cause", ignore=False)
    assert refuse
    assert message == (
        "example-linux: cannot read host limits: cause; "
        "pass --ignore-limits to launch anyway"
    )


@pytest.mark.parametrize("found", ["cause", limits.Reading(9, 8, 1000, 500, 0, 0)])
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
    monkeypatch.setattr(agent.settings, "load", lambda: CONFIG)
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
    host.configure(limits="9 4 1000 900 100 0")
    assert launch(host) == 1
    assert "load 9 >= limit 4" in capsys.readouterr().err
    assert len(host.records("ssh", limits=True)) == 1
    assert host.records("ssh") == []
    assert not driver.metadata_path("sample").exists()


def test_ignore_limits_launches_warns_and_is_recorded(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(limits="9 4 1000 900 100 0", capacity="1 4 1000 0 100 0")
    assert launch(host, "--ignore-limits") == 0
    assert "warning: example-linux: load 9 >= limit 4" in capsys.readouterr().err
    assert json.loads(driver.metadata_path("sample").read_text())["ignore_limits"]
    assert driver.load(CONFIG, "sample").ignore_limits
    assert len(list((host.root / "admission").glob("claim.*"))) == 1


def test_capacity_refusal_leaves_no_claim_and_allows_retry(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    (host.state / "earlier.owner").write_text("earlier-owner\n")
    host.configure(capacity="1 4 33554432 12582911 100 0")
    assert launch(host) == 1
    assert (
        "example-linux: live runs 1, free memory 12582911 KiB: insufficient "
        "memory for a 6144 MiB reservation; pass --ignore-limits to launch anyway"
        in capsys.readouterr().err
    )
    assert not driver.metadata_path("sample").exists()
    assert not (host.state / "sample.owner").exists()
    assert not (host.state / "sample.request.json").exists()
    assert not any("new-session" in str(r["args"]) for r in host.records("tmux"))

    (host.state / "earlier.rc").write_text("0\n")
    host.configure(capacity="1 4 33554432 6291456 100 0")
    assert launch(host) == 0


def test_capacity_refuses_a_failed_fresh_read(
    host: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    host.configure(capacity_read_fails=True)
    assert launch(host) == 1
    assert "live runs 0, free memory unknown: cannot read host limits" in (
        capsys.readouterr().err
    )
    assert not driver.metadata_path("sample").exists()


@pytest.mark.parametrize("completed", [False, True])
def test_supervisor_releases_only_its_completed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, completed: bool
) -> None:
    shared = tmp_path / "admission"
    shared.mkdir()
    monkeypatch.setattr(ownership, "ADMISSION_DIR", str(shared))
    files = runfiles.RunFiles(tmp_path / "state", "sample")
    files.write("owner", "example-owner\n")
    digest = hashlib.sha256(b"example-owner\n").hexdigest()
    claim = shared / f"claim.{os.getuid()}.sample.{digest}"
    claim.touch()
    unrelated = claim.with_name(claim.name + "-other")
    unrelated.touch()
    with ownership.reservation(files, owned=True):
        assert claim.is_file()
        if completed:
            files.write("rc", "0\n")
    assert claim.is_file() is not completed
    assert unrelated.is_file()


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


def test_check_limits_runs_without_a_launch(host: Remote) -> None:
    host.configure(limits="9 4 1000 900 100 0")
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
