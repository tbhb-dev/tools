"""Flag private repository names in a public pull request draft.

`private_name_problems` is the pure check. A token is a run of ASCII letters,
digits, `_` and `-`, joined by single dots, that no such character or dot
touches on either side, so `example-private-tools` doesn't match
`example-private` and a sentence-ending dot isn't part of the token. The check
ignores case and a trailing `.git`, so clone URLs match. It flags a token that
ends in `-private` and doesn't start with `-`, and a token that equals a
configured name.

`draft_problems` adds the configuration: the optional `[pr_check]` table of
the user config, key `private_repos`. It skips the check in a private working
repository, where such names are fine.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tbhbdev import userconfig
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tbhbdev.alpha.pull_request import Problem

SUFFIX = "-private"
MESSAGE = "'{}' names a private repository; describe it generically instead"
TOKEN = re.compile(r"(?<![\w.-])[\w-]+(?:\.[\w-]+)*(?![\w-])", re.ASCII)


def private_name_problems(
    lines: Sequence[str], names: Sequence[str], first: int = 1
) -> list[Problem]:
    """One problem per distinct private-looking token on each line.

    LINES start at line number FIRST. NAMES are the configured private
    repository names; a token that ends in `-private` is flagged without one.
    Matching ignores case.
    """
    wanted = {name.lower() for name in names}
    return [
        (number, MESSAGE.format(token))
        for number, line in enumerate(lines, first)
        for token in dict.fromkeys(TOKEN.findall(line))
        if _is_private(token, wanted)
    ]


def _is_private(token: str, wanted: set[str]) -> bool:
    """Whether TOKEN, minus a trailing `.git`, is a private name."""
    name = token.lower().removesuffix(".git")
    return (name.endswith(SUFFIX) and not name.startswith("-")) or name in wanted


def _configured() -> list[str]:
    """The configured private names; none when the config can't supply them."""
    try:
        return userconfig.optional_strings("pr_check", "private_repos") or []
    except userconfig.ConfigError, OSError, UnicodeDecodeError:
        return []


def _origin_name() -> str | None:
    """The repository name in `origin`'s URL, or None when git can't say."""
    code, url = git("config", "--get", "remote.origin.url")
    if code != 0:
        return None
    name = re.split(r"[/:]", url.strip().rstrip("/"))[-1].removesuffix(".git")
    return name.lower() or None


def private_names() -> list[str] | None:
    """The names to flag, or None when the working repository is private.

    A repository is private when its name ends in `-private` or is configured.
    An unreadable `origin` counts as public, so the check runs. A config the
    loader can't read gives no names; the suffix rule still applies.
    """
    names = _configured()
    origin = _origin_name()
    if origin is not None and (
        origin.endswith(SUFFIX) or origin in {name.lower() for name in names}
    ):
        return None
    return names


def draft_problems(lines: Sequence[str], first: int) -> list[Problem]:
    """The private-name problems of a rendered draft, if the check is on."""
    names = private_names()
    return [] if names is None else private_name_problems(lines, names, first)
