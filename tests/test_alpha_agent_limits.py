"""Launch limits: pure decisions, the remote script and the launch."""

from __future__ import annotations

import copy
import dataclasses
import json
import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import Remote, setup
from agent_example import LINUX, MACOS, MARKER, settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, taint_store
from tbhbdev.alpha.agent.decisions import limits, remote, taint
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent.model import Settings

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


def run_script(script: str) -> tuple[int, str]:
    done = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=False
    )
    return done.returncode, done.stdout


VM_STATS = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                        262144.
Pages inactive:                   1048576.
Pages speculative:                 589824.
Pages purgeable:                   524288.
"""


def macos_script(
    stats: str = VM_STATS,
    *,
    failed: str = "",
    load: str = "{ 0.5 0.8 0.9 }",
    memory: str = "68719476736",
) -> str:
    return f"""uname() {{ printf '%s\\n' Darwin; }}
sysctl() {{
    test "$2" != {shlex.quote(failed)} || return 1
    case "$2" in
        vm.loadavg) printf '%s\\n' {shlex.quote(load)} ;;
        hw.logicalcpu) printf '%s\\n' 4 ;;
        hw.memsize) printf '%s\\n' {shlex.quote(memory)} ;;
        vm.swapusage) printf '%s\\n' 'total = 100.00M  used = 87.00M  free = 13.00M' ;;
        *) return 1 ;;
    esac
}}
vm_stat() {{
    printf '%s\\n' {shlex.quote(stats)}
    test {shlex.quote(failed)} != vm_stat
}}
""" + remote.limits()


@pytest.mark.parametrize("failed", ["", "vm.swapusage"])
def test_macos_launches_with_available_memory_despite_swap(failed: str) -> None:
    code, out = run_script(macos_script(failed=failed))
    assert code == 0
    reading = limits.parse(out)
    assert reading == limits.Reading(0.5, 4, 64 * 1024**2, 29 * 1024**2, 0, 0)
    assert limits.decide(MACOS, reading, ignore=False) == (False, "")


def test_macos_uses_the_reported_page_size_and_refuses_low_memory() -> None:
    code, out = run_script(macos_script(VM_STATS.replace("16384", "4096")))
    assert code == 0
    reading = limits.parse(out)
    assert reading == limits.Reading(0.5, 4, 64 * 1024**2, 29 * 256 * 1024, 0, 0)
    assert limits.decide(MACOS, reading, ignore=False) == (
        True,
        (
            f"{MACOS.name}: memory available 11% < 20%; "
            "pass --ignore-limits to launch anyway"
        ),
    )


@pytest.mark.parametrize(
    "probe", ["vm.loadavg", "hw.logicalcpu", "hw.memsize", "vm_stat"]
)
def test_a_failed_macos_read_ends_the_script_nonzero(probe: str) -> None:
    assert run_script(macos_script(failed=probe)) == (1, "")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("page size of", "page size unknown"),
        ("16384", "0"),
        ("16384", "bad"),
        ("Pages free:", "Pages used:"),
        ("Pages inactive:", "Pages free:"),
        ("Pages speculative:", "Pages free:"),
        ("262144.", "bad"),
        ("589824.", "-1."),
    ],
)
def test_a_malformed_macos_memory_read_ends_the_script_nonzero(
    old: str, new: str
) -> None:
    assert run_script(macos_script(VM_STATS.replace(old, new))) == (1, "")


def test_a_malformed_macos_sysctl_read_ends_the_script_nonzero() -> None:
    assert run_script(macos_script(load="")) == (1, "")
    assert run_script(macos_script(memory="68719476736 bytes")) == (1, "")


def linux_script(tmp_path: Path, *, old: str = "", new: str = "") -> str:
    loadavg = tmp_path / "loadavg"
    loadavg.write_text("0.5 0.8 0.9 1/1 1\n")
    script = remote.limits()
    assert script.startswith("# tbhbdev-limits\n")
    if old:
        script = script.replace(old, new)
    return (
        "uname() { printf '%s\\n' Linux; }\nnproc() { printf '%s\\n' 4; }\n"
        + script.replace("/proc/loadavg", shlex.quote(str(loadavg))).replace(
            "/proc/meminfo", shlex.quote(str(tmp_path / "meminfo"))
        )
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/proc/loadavg", "/nonexistent"),
        ("$(nproc)", "$(false)"),
    ],
)
def test_a_failed_linux_read_ends_the_script_nonzero(
    old: str, new: str, tmp_path: Path
) -> None:
    script = linux_script(tmp_path, old=old, new=new)
    assert run_script(script) == (1, "")


MEMINFO = {
    "MemTotal": "10 kB",
    "MemAvailable": "5 kB",
    "SwapTotal": "0 kB",
    "SwapFree": "0 kB",
}


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
    assert run_script(linux_script(tmp_path)) == (1, "")


def test_a_swapless_linux_host_with_zero_fields_is_readable(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(
        "".join(f"{name}: {value}\n" for name, value in MEMINFO.items())
    )
    code, out = run_script(linux_script(tmp_path))
    assert code == 0
    assert limits.parse(out) == limits.Reading(0.5, 4, 10, 5, 0, 0)


def test_linux_refuses_high_swap_with_available_memory(tmp_path: Path) -> None:
    fields = MEMINFO | {"SwapTotal": "100 kB", "SwapFree": "13 kB"}
    (tmp_path / "meminfo").write_text(
        "".join(f"{name}: {value}\n" for name, value in fields.items())
    )
    code, out = run_script(linux_script(tmp_path))
    assert code == 0
    reading = limits.parse(out)
    assert reading == limits.Reading(0.5, 4, 10, 5, 100, 87)
    assert limits.decide(LINUX, reading, ignore=False) == (
        True,
        f"{LINUX.name}: swap used 87% > 80%; pass --ignore-limits to launch anyway",
    )


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    taint_store.record_clearance(
        taint.Clearance("example-linux", "Test host rebuilt", "2026-11-10T00:00:00Z")
    )

    def load_settings() -> Settings:
        return CONFIG

    monkeypatch.setattr(agent.settings, "load", load_settings)
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
    host.configure(limits="9 4 1000 900 100 0")
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
