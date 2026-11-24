"""Which launching sessions may start a reviewer-role run; no I/O.

Any named session may launch reviews, including for its own repository.
Review tier rules live in briefs; this check only requires a session name.
"""

from __future__ import annotations

import re

from tbhbdev.alpha.agent.model import Invalid, Profile

REVIEWER_ROLE = "reviewer"
ASK = "name the launching session via the tmux pane title"
_GLYPHS = re.compile(r"^[^0-9A-Za-z]+")


def session_name(title: str) -> str | None:
    """A pane title without the status glyph and spaces before the name."""
    name = _GLYPHS.sub("", title.strip()).strip()
    return name or None


def is_reviewer(role: str | None, profile: Profile) -> bool:
    """A review-profile run counts too, so omitting the role doesn't bypass the stop."""
    return role == REVIEWER_ROLE or profile is Profile.REVIEW


def check(launcher: str | None) -> None:
    """Raise `Invalid` unless the launching session has a name."""
    if launcher is None:
        message = (
            "a reviewer-role run needs the launching session's name, from the "
            f"tmux pane title; TMUX_PANE or its title is missing, so {ASK}"
        )
        raise Invalid(message)
