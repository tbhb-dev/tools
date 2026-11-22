"""Named refs are checked before transport and again at the host boundary."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import execution_arguments
from agent_example import settings
from agent_host_example import run_request
from test_alpha_agent_request import BUILD

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, worktree
from tbhbdev.alpha.agent.decisions import request
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path


BAD_REFS = [
    "",
    "main",
    "HEAD",
    "--all",
    "+refs/heads/main:refs/heads/main",
    "refs/tags/v1",
    "refs/pull/1/merge",
    "refs/pull//head",
    "refs/pull/-1/head",
    "refs/pull/1x/head",
    "refs/pull/\uff11/head",
    "refs/pull/1/head/extra",
    "refs/heads/",
    "refs/heads//main",
    "refs/heads/main/",
    "refs/heads/a//b",
    "refs/heads/a..b",
    "refs/heads/../main",
    "refs/heads/main.lock",
    "refs/heads/-main",
    "refs/heads/a b",
    "refs/heads/a\nb",
    "refs/heads/main\n",
    "refs/heads/a\x00b",
    "refs/heads/a:b",
    "refs/heads/a*b",
    "refs/heads/a?b",
    "refs/heads/a[b",
    "refs/heads/a\\b",
    "refs/heads/a@{b",
    "refs/heads/\u00e9",
]
GOOD_REFS = [
    "refs/pull/0/head",
    "refs/pull/391/head",
    "refs/pull/001/head",
    "refs/heads/main",
    "refs/heads/Fix_391/v2.0-test",
    "refs/heads/a",
    "refs/heads/feature.locked",
    "refs/heads/feature/-part",
]


@pytest.mark.parametrize("ref", BAD_REFS)
def test_invalid_ref_is_refused_by_controller_and_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    before = list(tmp_path.rglob("*"))
    args = agent.parser("agent").parse_args(
        execution_arguments("brief.md", f"--fetch-ref={ref}")
    )
    expected = f"request fetch_refs contains invalid ref {ref!r}"
    with pytest.raises(Invalid) as caught:
        driver.prepare(settings(), args)
    assert str(caught.value) == expected
    with pytest.raises(Invalid) as caught:
        request.parse(BUILD | {"fetch_refs": [ref]}, host_allows_unsandboxed=True)
    assert str(caught.value) == expected
    forged = dataclasses.replace(run_request(), fetch_refs=(ref,))
    with pytest.raises(Invalid) as caught:
        worktree.fetch_commit(tmp_path, forged)
    assert str(caught.value) == expected
    with pytest.raises(Invalid) as caught:
        worktree.create(tmp_path, "sample", forged, (forged.repo,))
    assert str(caught.value) == expected
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("profile", ["build", "review"])
def test_eight_refs_round_trip_for_any_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    options = [part for ref in GOOD_REFS for part in ("--fetch-ref", ref)]
    if profile == "review":
        options += ["--ref", "a" * 40]
    args = agent.parser("agent").parse_args(
        execution_arguments("brief.md", "--profile", profile, *options)
    )
    run = driver.prepare(settings(), args).request
    assert run.fetch_refs == tuple(GOOD_REFS)
    wire = json.loads(json.dumps(request.to_dict(run)))
    assert wire["fetch_refs"] == GOOD_REFS
    assert request.parse(wire, host_allows_unsandboxed=False) == run


def test_ninth_ref_is_refused_by_controller_and_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    before = list(tmp_path.rglob("*"))
    refs = [*GOOD_REFS, "refs/heads/ninth"]
    options = [part for ref in refs for part in ("--fetch-ref", ref)]
    args = agent.parser("agent").parse_args(execution_arguments("brief.md", *options))
    with pytest.raises(Invalid, match=r"^request fetch_refs allows at most 8 refs$"):
        driver.prepare(settings(), args)
    with pytest.raises(Invalid, match=r"^request fetch_refs allows at most 8 refs$"):
        request.parse(BUILD | {"fetch_refs": refs}, host_allows_unsandboxed=True)
    forged = dataclasses.replace(run_request(), fetch_refs=tuple(refs))
    with pytest.raises(Invalid, match=r"^request fetch_refs allows at most 8 refs$"):
        worktree.fetch_commit(tmp_path, forged)
    with pytest.raises(Invalid, match=r"^request fetch_refs allows at most 8 refs$"):
        worktree.create(tmp_path, "sample", forged, (forged.repo,))
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("value", [None, "refs/heads/main", {}, (), 1, [1], [None]])
def test_wire_refs_must_be_a_list_of_strings(value: object) -> None:
    with pytest.raises(
        Invalid, match=r"^request fetch_refs must be a list of strings$"
    ):
        request.parse(BUILD | {"fetch_refs": value}, host_allows_unsandboxed=True)


def test_exec_help_describes_fetch_refs(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent.main(["exec", "--help"], "agent") == 0
    help_text = capsys.readouterr().out
    assert "--fetch-ref REF" in help_text
    assert "origin/fetched/..." in help_text
    assert "repeat up to 8 times" in help_text
