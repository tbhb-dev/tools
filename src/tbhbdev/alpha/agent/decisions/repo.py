"""Resolving a repo argument to its clone path under the home directory.

The argument is `org/repo` or `<git host>/<org>/<repo>`, with `github.com` as
the default git host. The clone is `Code/<git host>/<org>/<repo>`. Nothing here
checks that the clone exists.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import PurePath

DEFAULT_GIT_HOST = "github.com"

# GitHub's rules: an owner is up to 39 letters, digits and single hyphens, and
# neither starts nor ends with a hyphen. A repo is up to 100 letters, digits,
# hyphens, underscores and dots.
OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_GIT_HOST = re.compile(rf"(?=.{{1,253}}\Z){_LABEL}(?:\.{_LABEL})*")


def parts(argument: str) -> tuple[str, str, str]:
    """The git host, owner and repo name that ARGUMENT names.

    Raises:
        Invalid: ARGUMENT is absolute, has an empty part or a `..` part, isn't
            `org/repo` or `<git host>/<org>/<repo>`, or has a part that
            GitHub's name rules or the host name rules refuse.
    """
    if argument.startswith("/"):
        message = f"repo {argument!r} must not be an absolute path"
        raise Invalid(message)
    names = argument.split("/")
    if "" in names:
        message = f"repo {argument!r} has an empty part"
        raise Invalid(message)
    if ".." in names:
        message = f"repo {argument!r} has a '..' part"
        raise Invalid(message)
    if len(names) == 2:  # noqa: PLR2004 -- org/repo
        names.insert(0, DEFAULT_GIT_HOST)
    if len(names) != 3:  # noqa: PLR2004 -- host/org/repo
        message = f"repo {argument!r} must be org/repo or <git host>/<org>/<repo>"
        raise Invalid(message)
    git_host, owner, name = names
    checks = (
        (git_host, _GIT_HOST, "git host"),
        (owner, OWNER, "owner"),
        (name, REPO, "name"),
    )
    for text, pattern, what in checks:
        if not pattern.fullmatch(text) or text == ".":
            message = f"repo {argument!r} has an invalid {what} {text!r}"
            raise Invalid(message)
    return git_host, owner, name


def clone_path(home: PurePath, argument: str) -> PurePath:
    """HOME's `Code/<git host>/<org>/<repo>` for ARGUMENT.

    Raises:
        Invalid: See `parts`.
    """
    return home.joinpath("Code", *parts(argument))
