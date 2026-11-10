"""Prune uses local metadata and a fake transport, never a live host."""

from __future__ import annotations

import dataclasses
import json
from argparse import Namespace
from collections import defaultdict
from typing import TYPE_CHECKING

import pytest
from agent_example import settings
from test_alpha_agent_request import BUILD, UNSANDBOXED

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, prune, transport
from tbhbdev.alpha.agent.decisions import prune_remote, request
from tbhbdev.alpha.agent.decisions.prune import Facts
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent.model import Host, Settings

CONFIG = settings()
OLD = "0\n1\n0\n3\n21\n"
UNMEASURED = "0\n1\n0\n-\n-\n"
CLEAN = driver.clean


class Inventory:
    def __init__(self) -> None:
        self.answers: dict[str, list[str | Exception]] = defaultdict(lambda: [OLD])
        self.calls: list[str] = []
        self.cleaned: list[tuple[str, bool]] = []

    def add(self, name: str, *, host: int = 0, unsandboxed: bool = False) -> driver.Run:
        run = driver.Run(
            name,
            CONFIG.hosts[host],
            request.parse(
                UNSANDBOXED if unsandboxed else BUILD, host_allows_unsandboxed=True
            ),
            "start",
            "owner",
        )
        driver.save(run)
        if unsandboxed:
            self.answers[name] = [UNMEASURED]
        return run

    def inventory(self, config: Settings, host: Host, script: str) -> str:
        assert config is CONFIG
        for name in prune.names():
            run = driver.load(CONFIG, name)
            if script == prune_remote.facts(
                run.request.repo,
                name,
                CONFIG.sockets_dir,
                owner=run.owner,
                measure=not run.request.unsandboxed,
            ):
                assert host == run.host
                self.calls.append(name)
                values = self.answers[name]
                result = values.pop(0) if len(values) > 1 else values[0]
                if isinstance(result, Exception):
                    raise result
                return result
        pytest.fail("unexpected remote script")

    def clean(self, config: Settings, run: driver.Run, *, force: bool) -> int:
        assert config is CONFIG
        self.cleaned.append((run.name, force))
        driver.archive_metadata(run.name, run.name)
        return 0


@pytest.fixture
def inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Inventory:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(agent.settings, "load", lambda: CONFIG)
    monkeypatch.setattr(prune.time, "time", lambda: 2000000)
    result = Inventory()
    monkeypatch.setattr(transport, "shell", result.inventory)
    monkeypatch.setattr(driver, "clean", result.clean)
    return result


def command(*options: str) -> int:
    return agent.main(["prune", *options], "example agent")


def test_empty_directory(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command() == 0
    assert capsys.readouterr() == ("", "")
    assert inventory.calls == []


def test_enumeration_and_dry_totals(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("z", "a", "b", "kept", "running", "unfinished", "recent"):
        inventory.add(name, host=1 if name == "a" else 0)
    inventory.answers["b"] = ["0\n2\n0\n5\n41\n"]
    inventory.answers["kept"] = ["1\n-\n1\n0\n0\n"]
    inventory.answers["running"] = ["1\n-\n0\n0\n0\n"]
    inventory.answers["unfinished"] = ["0\n-\n0\n0\n0\n"]
    inventory.answers["recent"] = ["0\n2000000\n0\n0\n0\n"]
    root = driver.directory()
    (root / "invalid.name.json").write_text("bad")
    (root / "folder.json").mkdir()
    (root / "link.json").symlink_to(root / "a.json")
    (root / "other.txt").write_text("bad")
    (root / "trash").mkdir()
    (root / "trash/archived.json").write_text("bad")
    assert command() == 0
    assert inventory.calls == ["a", "b", "kept", "recent", "running", "unfinished", "z"]
    assert inventory.cleaned == []
    assert capsys.readouterr() == (
        (
            "prune: example-macos a bytes=21 files=3\n"
            "prune: example-linux b bytes=41 files=5\n"
            "kept: example-linux kept\nrunning: example-linux running\n"
            "unfinished: example-linux unfinished\n"
            "prune: example-linux z bytes=21 files=3\n"
            "would free: example-linux runs=2 files=8 bytes=62\n"
            "would free: example-macos runs=1 files=3 bytes=21\n"
        ),
        "",
    )


@pytest.mark.parametrize("bad", ["json", "host"])
def test_invalid_metadata_continues(
    inventory: Inventory, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    inventory.add("a")
    inventory.add("z")
    path = driver.metadata_path("a")
    if bad == "json":
        path.write_text("bad")
    else:
        record = json.loads(path.read_text())
        record["host"] = "gone"
        path.write_text(json.dumps(record))
    # The fake only resolves the valid run; load failures stay local.

    def read_valid(config: Settings, host: Host, script: str) -> str:
        assert "z.owner" in script
        assert config is CONFIG
        assert host == CONFIG.hosts[0]
        return OLD

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(transport, "shell", read_valid)
        assert command() == 1
    out, err = capsys.readouterr()
    assert (
        out == "prune: example-linux z bytes=21 files=3\n"
        "would free: example-linux runs=1 files=3 bytes=21\n"
    )
    assert err == "prune: invalid metadata: a\n"


@pytest.mark.parametrize(
    "text",
    [
        "",
        OLD + "extra",
        OLD.rstrip(),
        "2\n1\n0\n3\n21\n",
        "0\n+1\n0\n3\n21\n",
        "0\n1\n2\n3\n21\n",
        "0\n1\n0\n-3\n21\n",
        "0\n1\n0\n3\n1.0\n",
        "0\n1\n0\n3\n٢\n",
        "0\n1\n0\n3\n21\n\n",
    ],
)
def test_strict_facts(
    inventory: Inventory, text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory.add("sample")
    inventory.answers["sample"] = [text]
    assert command() == 1
    assert capsys.readouterr() == (
        "",
        "prune failed: example-linux sample: invalid prune facts\n",
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (OLD, Facts(session=False, rc_mtime=1, keep=False, files=3, bytes=21)),
        (
            "1\n-\n1\n0\n0\n",
            Facts(session=True, rc_mtime=None, keep=True, files=0, bytes=0),
        ),
        (
            "0\n0\n0\n12\n345\n",
            Facts(session=False, rc_mtime=0, keep=False, files=12, bytes=345),
        ),
    ],
)
def test_parsed_facts(inventory: Inventory, text: str, expected: Facts) -> None:
    run = inventory.add("sample")
    inventory.answers["sample"] = [text]
    assert prune.read(CONFIG, run) == expected


def test_largest_global_order_and_limit(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("z", "b", "a", "small", "c", "unmeasured"):
        inventory.add(
            name, host=1 if name == "b" else 0, unsandboxed=name in {"a", "unmeasured"}
        )
    inventory.answers["z"] = ["1\n-\n1\n5\n31\n"]
    inventory.answers["small"] = ["0\n-\n0\n100\n1\n"]
    order = ["z", "unmeasured", "small", "c", "b", "a"]
    monkeypatch.setattr(prune, "names", lambda: iter(order))
    assert command("--largest", "3") == 0
    assert capsys.readouterr() == (
        (
            "largest: example-linux z bytes=31 files=5\n"
            "largest: example-macos b bytes=21 files=3\n"
            "largest: example-linux c bytes=21 files=3\n"
            "unsandboxed: example-linux a (not measured)\n"
            "unsandboxed: example-linux unmeasured (not measured)\n"
        ),
        "",
    )
    assert inventory.calls == order
    assert inventory.cleaned == []


@pytest.mark.parametrize("stage", ["facts", "recheck", "clean"])
@pytest.mark.parametrize(
    "error", [Invalid, transport.TransportError, transport.CollisionError]
)
def test_errors_continue_and_sanitize(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
    error: type[Exception],
) -> None:
    inventory.add("a")
    inventory.add("z")
    failure = error("bad\x1b\x00 message")
    if stage == "facts":
        inventory.answers["a"] = [failure]
    elif stage == "recheck":
        inventory.answers["a"] = [OLD, failure]
    else:

        def refuse(config: Settings, run: driver.Run, *, force: bool) -> int:
            if run.name == "a":
                raise failure
            return inventory.clean(config, run, force=force)

        monkeypatch.setattr(driver, "clean", refuse)
    assert command("--apply", "--force") == 1
    out, err = capsys.readouterr()
    assert err == "prune failed: example-linux a: bad message\n"
    prefix = "" if stage == "facts" else "prune: example-linux a bytes=21 files=3\n"
    assert (
        out == prefix + "prune: example-linux z bytes=21 files=3\n"
        "removed: example-linux runs=1 files=3 bytes=21\n"
    )
    assert inventory.cleaned == [("z", False)]


@pytest.mark.parametrize(
    "changed",
    [
        "0\n1\n1\n3\n21\n",
        "1\n1\n0\n3\n21\n",
        "0\n-\n0\n3\n21\n",
        "0\n2000000\n0\n3\n21\n",
    ],
)
def test_changed_selection_skips_and_continues(
    inventory: Inventory, capsys: pytest.CaptureFixture[str], changed: str
) -> None:
    inventory.add("a")
    inventory.add("z")
    inventory.answers["a"] = [OLD, changed]
    assert command("--apply") == 1
    assert capsys.readouterr() == (
        (
            "prune: example-linux a bytes=21 files=3\n"
            "prune: example-linux z bytes=21 files=3\n"
            "removed: example-linux runs=1 files=3 bytes=21\n"
        ),
        "prune changed, skipped: example-linux a\n",
    )
    assert inventory.cleaned == [("z", False)]


def test_apply_remeasures_and_force_only_bypasses_keep(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory.add("sample")
    inventory.answers["sample"] = ["0\n1\n1\n3\n21\n", "0\n1\n1\n8\n90\n"]
    assert command("--apply", "--force") == 0
    assert inventory.cleaned == [("sample", False)]
    assert inventory.calls == ["sample", "sample"]
    assert capsys.readouterr() == (
        (
            "prune: example-linux sample bytes=21 files=3\n"
            "removed: example-linux runs=1 files=8 bytes=90\n"
        ),
        "",
    )
    assert not driver.metadata_path("sample").exists()


def test_largest_failure_still_prints_valid_runs(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory.add("a")
    inventory.add("z")
    inventory.answers["a"] = [transport.CollisionError("not owner")]
    assert command("--largest", "99") == 1
    assert capsys.readouterr() == (
        "largest: example-linux z bytes=21 files=3\n",
        "prune failed: example-linux a: not owner\n",
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--older-than", "-1"],
        ["--older-than", "1000000"],
        ["--older-than", "1.2"],
        ["--older-than", "٢"],
        ["--older-than", "+1"],
        ["--largest", "0"],
        ["--largest", "000000"],
        ["--largest", "bad"],
        ["--largest", "1", "--apply"],
        ["--force", "--largest", "1"],
        ["--wat"],
        ["--older", "1"],
        ["--largest"],
    ],
)
def test_parser_rejects_bad_values(options: list[str]) -> None:
    assert command(*options) == 2


def test_parser_defaults_and_limits() -> None:
    parsed = agent.parser("example").parse_args(["prune"])
    assert (parsed.older_than, parsed.largest, parsed.apply, parsed.force) == (
        14,
        None,
        False,
        False,
    )
    parsed = agent.parser("example").parse_args(
        ["prune", "--older-than", "000000", "--largest", "999999"]
    )
    assert (parsed.older_than, parsed.largest) == (0, 999999)
    assert agent.prune_number("999999") == 999999
    assert agent.prune_positive("000001") == 1


@pytest.mark.parametrize("refusal", ["missing-branch", "fallback"])
def test_clean_refusals_remain_even_with_prune_force(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refusal: str,
) -> None:
    monkeypatch.setattr(driver, "clean", CLEAN)
    inventory.add("sample")
    monkeypatch.setattr(
        transport,
        "read",
        lambda *_a, **_k: transport.Reading(
            "done", 0, launched=True, session=False, text=""
        ),
    )

    def refuse(config: Settings, host: Host, script: str) -> str:
        assert config is CONFIG
        assert host == CONFIG.hosts[0]
        if "ls-remote" in script:
            return (
                "" if refusal == "missing-branch" else "sha\trefs/heads/agent/sample\n"
            )
        assert "fallback rc requires --force" in script
        msg = "fallback rc refused"
        raise transport.TransportError(msg)

    monkeypatch.setattr(transport, "shell", refuse)
    monkeypatch.setattr(
        prune,
        "read",
        lambda *_a: Facts(session=False, rc_mtime=1, keep=True, files=3, bytes=21),
    )
    assert command("--apply", "--force") == 1
    out, err = capsys.readouterr()
    expected = "prune: example-linux sample bytes=21 files=3\n"
    if refusal == "missing-branch":
        assert out == expected + "removed: example-linux runs=0 files=0 bytes=0\n"
        assert (
            err == "prune failed: example-linux sample: branch agent/sample "
            "does not exist on origin; pass --force to clean anyway\n"
        )
    else:
        assert out == (
            expected + "branch exists on origin\n"
            "removed: example-linux runs=0 files=0 bytes=0\n"
        )
        assert err == "prune failed: example-linux sample: fallback rc refused\n"
    assert driver.metadata_path("sample").exists()


def test_dynamic_output_uses_terminal_text(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = inventory.add("sample")
    run.host = dataclasses.replace(run.host, name="host\x00")
    run.name = "name\x00"
    monkeypatch.setattr(driver, "load", lambda *_a: run)
    monkeypatch.setattr(
        prune,
        "read",
        lambda *_a: Facts(session=False, rc_mtime=1, keep=False, files=1, bytes=2),
    )
    assert command() == 0
    assert capsys.readouterr() == (
        "prune: host name bytes=2 files=1\nwould free: host runs=1 files=1 bytes=2\n",
        "",
    )


@pytest.mark.parametrize("mode", ["kept", "largest", "changed"])
def test_other_dynamic_output_uses_terminal_text(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    run = inventory.add("sample")
    run.host = dataclasses.replace(run.host, name="host\x00")
    run.name = "name\x00"
    monkeypatch.setattr(driver, "load", lambda *_a: run)
    values = iter(
        [
            Facts(session=False, rc_mtime=1, keep=mode == "kept", files=1, bytes=2),
            Facts(session=True, rc_mtime=1, keep=False, files=1, bytes=2),
        ]
    )
    monkeypatch.setattr(prune, "read", lambda *_a: next(values))
    options = {"kept": [], "largest": ["--largest", "1"], "changed": ["--apply"]}
    assert command(*options[mode]) == (1 if mode == "changed" else 0)
    outputs = {
        "kept": ("kept: host name\n", ""),
        "largest": ("largest: host name bytes=2 files=1\n", ""),
        "changed": (
            "prune: host name bytes=2 files=1\nremoved: host runs=0 files=0 bytes=0\n",
            "prune changed, skipped: host name\n",
        ),
    }
    assert capsys.readouterr() == outputs[mode]


def test_invalid_metadata_output_uses_terminal_text(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory.add("sample")
    monkeypatch.setattr(prune, "names", lambda: iter(["name\x00"]))

    def invalid(*_args: object) -> driver.Run:
        msg = "untrusted\x00"
        raise Invalid(msg)

    monkeypatch.setattr(driver, "load", invalid)
    assert command() == 1
    assert capsys.readouterr() == ("", "prune: invalid metadata: name\n")


def test_custom_age_reaches_selection(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory.add("sample")
    assert command("--older-than", "999999") == 0
    assert capsys.readouterr() == ("", "")
    assert inventory.cleaned == []


@pytest.mark.parametrize(
    "options", [[], ["--force"], ["--apply"], ["--apply", "--force"]]
)
def test_unsandboxed_only_skip(
    inventory: Inventory, capsys: pytest.CaptureFixture[str], options: list[str]
) -> None:
    inventory.add("sample", unsandboxed=True)
    assert command(*options) == 0
    assert capsys.readouterr() == (
        (
            "unsandboxed: example-linux sample (not measured; "
            "clean with agent clean --force)\n"
        ),
        "",
    )
    assert inventory.cleaned == []
    assert inventory.calls == ["sample"]
    assert driver.metadata_path("sample").exists()


@pytest.mark.parametrize("apply", [False, True])
def test_unsandboxed_excluded_from_totals(
    inventory: Inventory, capsys: pytest.CaptureFixture[str], *, apply: bool
) -> None:
    inventory.add("a", unsandboxed=True)
    inventory.add("z")
    assert command(*(["--apply"] if apply else [])) == 0
    action = "removed" if apply else "would free"
    assert capsys.readouterr() == (
        (
            "unsandboxed: example-linux a (not measured; "
            "clean with agent clean --force)\n"
            "prune: example-linux z bytes=21 files=3\n"
            f"{action}: example-linux runs=1 files=3 bytes=21\n"
        ),
        "",
    )
    assert inventory.cleaned == ([("z", False)] if apply else [])


@pytest.mark.parametrize("sanitize", [False, True])
@pytest.mark.parametrize("prior_error", [False, True])
def test_recheck_unsandboxed_is_non_error(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    sanitize: bool,
    prior_error: bool,
) -> None:
    run = inventory.add("sample")
    if sanitize:
        run.host = dataclasses.replace(run.host, name="host\x00")
        run.name = "name\x00"

    def load(config: Settings, name: str) -> driver.Run:
        assert config is CONFIG
        if name == "invalid":
            msg = "invalid metadata"
            raise Invalid(msg)
        return run

    monkeypatch.setattr(driver, "load", load)
    monkeypatch.setattr(
        prune,
        "names",
        lambda: iter(["invalid", "sample"] if prior_error else ["sample"]),
    )
    reads = []

    def changing_read(config: Settings, current: driver.Run) -> Facts:
        assert config is CONFIG
        assert current is run
        reads.append(current.name)
        if len(reads) == 2:
            current.request = request.parse(UNSANDBOXED, host_allows_unsandboxed=True)
        return Facts(session=False, rc_mtime=1, keep=False, files=3, bytes=21)

    monkeypatch.setattr(prune, "read", changing_read)
    assert command("--apply", "--force") == (1 if prior_error else 0)
    host, name = ("host", "name") if sanitize else ("example-linux", "sample")
    assert capsys.readouterr() == (
        (
            f"prune: {host} {name} bytes=21 files=3\n"
            f"unsandboxed: {host} {name} (not measured; "
            "clean with agent clean --force)\n"
            f"removed: {host} runs=0 files=0 bytes=0\n"
        ),
        "prune: invalid metadata: invalid\n" if prior_error else "",
    )
    assert len(reads) == 2
    assert inventory.cleaned == []


@pytest.mark.parametrize("largest", [False, True])
def test_unsandboxed_output_uses_terminal_text(
    inventory: Inventory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    largest: bool,
) -> None:
    run = inventory.add("sample", unsandboxed=True)
    run.host = dataclasses.replace(run.host, name="host\x00")
    run.name = "name\x00"
    monkeypatch.setattr(driver, "load", lambda *_a: run)
    monkeypatch.setattr(
        prune,
        "read",
        lambda *_a: Facts(session=False, rc_mtime=1, keep=False, files=1, bytes=2),
    )
    assert command(*(["--largest", "1"] if largest else [])) == 0
    suffix = (
        "not measured" if largest else "not measured; clean with agent clean --force"
    )
    assert capsys.readouterr() == (
        f"unsandboxed: host name ({suffix})\n",
        "",
    )


@pytest.mark.parametrize(
    "text",
    [OLD, "0\n1\n0\n-\n21\n", "0\n1\n0\n3\n-\n", "0\n1\n0\n?\n?\n"],
)
def test_unsandboxed_facts_require_markers(
    inventory: Inventory, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    inventory.add("sample", unsandboxed=True)
    inventory.answers["sample"] = [text]
    assert command() == 1
    assert capsys.readouterr() == (
        "",
        "prune failed: example-linux sample: invalid prune facts\n",
    )


def test_sandboxed_facts_refuse_unmeasured_markers(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory.add("sample")
    inventory.answers["sample"] = [UNMEASURED]
    assert command() == 1
    assert capsys.readouterr() == (
        "",
        "prune failed: example-linux sample: invalid prune facts\n",
    )


def test_unsandboxed_parsed_facts(inventory: Inventory) -> None:
    run = inventory.add("sample", unsandboxed=True)
    assert prune.read(CONFIG, run) == Facts(
        session=False, rc_mtime=1, keep=False, files=0, bytes=0
    )


def test_unsandboxed_recheck_does_not_measure(
    inventory: Inventory, capsys: pytest.CaptureFixture[str]
) -> None:
    run = inventory.add("sample", unsandboxed=True)
    assert prune.remove(CONFIG, run, Namespace(older_than=14, force=True)) == 0
    assert inventory.calls == ["sample"]
    assert inventory.cleaned == []
    assert capsys.readouterr() == (
        (
            "unsandboxed: example-linux sample (not measured; "
            "clean with agent clean --force)\n"
        ),
        "",
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("0\n1\n1\n-\n-\n", "kept"),
        ("1\n1\n0\n-\n-\n", "running"),
        ("0\n-\n0\n-\n-\n", "unfinished"),
        ("0\n2000000\n0\n-\n-\n", "recent"),
    ],
)
@pytest.mark.parametrize("apply", [False, True])
def test_unsandboxed_protections_stay_unmeasured(
    inventory: Inventory,
    capsys: pytest.CaptureFixture[str],
    text: str,
    reason: str,
    *,
    apply: bool,
) -> None:
    inventory.add("sample", unsandboxed=True)
    inventory.answers["sample"] = [text]
    assert command(*(["--apply"] if apply else [])) == 0
    output = "" if reason == "recent" else f"{reason}: example-linux sample\n"
    assert capsys.readouterr() == (output, "")
    assert inventory.calls == ["sample"]
    assert inventory.cleaned == []
