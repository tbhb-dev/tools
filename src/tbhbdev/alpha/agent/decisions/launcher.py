"""Decide whether a session may commission a review of a recorded build."""

from __future__ import annotations

import re

from tbhbdev.alpha.agent.model import NAME, Harness, Invalid, Profile

REVIEWER_ROLE = "reviewer"
PULL_HEAD = re.compile(r"refs/pull/([1-9][0-9]*)/head")
NO_BUILD = "no build run recorded for this review repository and ref"
_GLYPHS = re.compile(r"^[^0-9A-Za-z]+")
_REVIEW_PROVIDERS = {Harness.CODEX: Harness.CLAUDE, Harness.CLAUDE: Harness.CODEX}


def session_name(title: str) -> str | None:
    """A pane title without the status glyph and spaces before the name."""
    name = _GLYPHS.sub("", title.strip()).strip()
    return name or None


def is_reviewer(role: str | None, profile: Profile) -> bool:
    """Apply the review launch rules even when the caller omits the role."""
    return role == REVIEWER_ROLE or profile is Profile.REVIEW


def full_ref(ref: str) -> str:
    return f"refs/heads/{ref}" if ref.startswith("agent/") else ref


def build_name(branch: str) -> str:
    """Find the run name in the branch the build launcher publishes."""
    prefix = "refs/heads/agent/"
    ref = full_ref(branch)
    name = ref.removeprefix(prefix)
    if not ref.startswith(prefix) or not NAME.fullmatch(name):
        raise Invalid(NO_BUILD)
    return name


def check(
    launcher: tuple[str | None, str | None],
    commissioner: tuple[str | None, str | None],
    build: Harness,
    reviewer: Harness,
) -> None:
    """Require the build's commissioner and the other review provider."""
    if launcher != commissioner:
        message = "only the session that commissioned this build may launch its review"
        raise Invalid(message)
    expected = _REVIEW_PROVIDERS.get(build)
    if expected is None:
        message = "review commissioning supports Codex and Claude builds"
        raise Invalid(message)
    if reviewer is not expected:
        message = f"a {build} build requires a {expected} review run"
        raise Invalid(message)
