"""Host admission uses actual leases under an isolated state directory."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agent_example import LINUX

from tbhbdev.alpha.agent import admission
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from pathlib import Path


def test_builds_leave_one_slot_for_reviews(tmp_path: Path) -> None:
    host = replace(LINUX, max_concurrent_runs=3)
    alternate_account = replace(host, account=host.accounts[-1])
    root = tmp_path / "admission"
    with ExitStack() as slots:
        for _ in range(2):
            slots.enter_context(admission.reserve(root, host, Profile.BUILD))
        with (
            pytest.raises(Invalid, match="reserved for reviews"),
            admission.reserve(root, host, Profile.BUILD),
        ):
            pytest.fail("build claimed the review slot")
        with (
            admission.reserve(root, alternate_account, Profile.REVIEW),
            pytest.raises(Invalid, match="max_concurrent_runs=3"),
            admission.reserve(root, host, Profile.REVIEW),
        ):
            pytest.fail("review exceeded the host limit")
        with admission.reserve(root, host, Profile.REVIEW):
            assert not (root / "review-1").exists()
    with admission.reserve(root, host, Profile.BUILD):
        assert not (root / "build-2").exists()


def test_reviews_can_use_shared_capacity(tmp_path: Path) -> None:
    host = replace(LINUX, max_concurrent_runs=2)
    root = tmp_path / "admission"
    with (
        admission.reserve(root, host, Profile.REVIEW),
        admission.reserve(root, host, Profile.REVIEW),
    ):
        assert (root / "review-1").exists()


@pytest.mark.parametrize("profile", list(Profile))
def test_zero_slots_disables_every_profile(tmp_path: Path, profile: Profile) -> None:
    host = replace(LINUX, max_concurrent_runs=0)
    with (
        pytest.raises(Invalid, match="max_concurrent_runs=0"),
        admission.reserve(tmp_path / "admission", host, profile),
    ):
        pytest.fail("disabled host admitted a run")


def test_a_sole_slot_admits_a_build_then_refuses(tmp_path: Path) -> None:
    host = replace(LINUX, max_concurrent_runs=1)
    root = tmp_path / "admission"
    with (
        admission.reserve(root, host, Profile.BUILD),
        pytest.raises(Invalid, match="max_concurrent_runs=1"),
        admission.reserve(root, host, Profile.REVIEW),
    ):
        pytest.fail("a second run took the only slot")


def test_widens_a_directory_this_account_owns(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    root.mkdir(mode=0o755)
    with admission.reserve(root, LINUX, Profile.REVIEW):
        assert stat.S_IMODE(root.stat().st_mode) == 0o1777


def test_refuses_a_directory_with_the_wrong_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = os.getuid() + 1
    monkeypatch.setattr(admission.os, "getuid", lambda: other)
    with (
        pytest.raises(Invalid, match="directory must have mode 1777"),
        admission.reserve(tmp_path, LINUX, Profile.REVIEW),
    ):
        pytest.fail("admitted a run in a directory with the wrong mode")


def test_refuses_a_slot_that_is_not_regular(tmp_path: Path) -> None:
    root = tmp_path / "admission"
    with admission.reserve(root, LINUX, Profile.REVIEW):
        (root / "review-1").mkdir()
        with (
            pytest.raises(Invalid, match="slot must be a regular file"),
            admission.reserve(root, LINUX, Profile.REVIEW),
        ):
            pytest.fail("admitted a run with an invalid slot")
