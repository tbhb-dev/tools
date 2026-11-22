"""Which launching sessions may start a reviewer-role run; no I/O.

The list is a bump against honest mistakes by cooperating sessions, such as an
owner session launching its own reviews. It is not a barrier against a session
that forges its name. Changing the list is a reviewed change to this file.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Mapping

REVIEWER_ROLE = "reviewer"
# Session name to the repositories it may review for; None means any repository.
# The user's launcher table adds to these.
REVIEW_LAUNCHERS: dict[str, frozenset[str] | None] = {
    "coord-review-1": None,
    "coord-review-2": None,
    "coord-review-3": None,
}
# Repositories whose owner session isn't named for the repository come from the
# user's launcher table. Any other repository is owned by the session with its name.
ASK = "ask coord to route the review"
_GLYPHS = re.compile(r"^[^0-9A-Za-z]+")


def session_name(title: str) -> str | None:
    """A pane title without the status glyph and spaces before the name."""
    name = _GLYPHS.sub("", title.strip()).strip()
    return name or None


def is_reviewer(role: str | None, profile: Profile) -> bool:
    """A review-profile run counts too, so omitting the role doesn't bypass the stop."""
    return role == REVIEWER_ROLE or profile is Profile.REVIEW


def owner(repo: str, exceptions: Mapping[str, str]) -> str:
    """The session that owns a repository given as ``org/name``."""
    name = repo.rpartition("/")[2]
    return exceptions.get(name, name)


def check(
    launcher: str | None,
    repo: str,
    launchers: Mapping[str, frozenset[str] | None] = REVIEW_LAUNCHERS,
    exceptions: Mapping[str, str] | None = None,
) -> None:
    """Raise `Invalid` unless the launching session may start a reviewer-role run."""
    if launcher is None:
        message = (
            "a reviewer-role run needs the launching session's name, from the "
            f"tmux pane title; TMUX_PANE or its title is missing, so {ASK}"
        )
        raise Invalid(message)
    if launcher not in launchers:
        message = f"session {launcher!r} may not launch a reviewer-role run; {ASK}"
        raise Invalid(message)
    repos = launchers[launcher]
    if repos is not None and repo not in repos:
        message = (
            f"session {launcher!r} may launch reviewer-role runs only for "
            f"{', '.join(sorted(repos))}, not {repo}; {ASK}"
        )
        raise Invalid(message)
    if launcher == owner(repo, exceptions or {}):
        message = (
            f"session {launcher!r} owns {repo} and may not launch its own "
            f"reviewer-role run; {ASK}"
        )
        raise Invalid(message)
