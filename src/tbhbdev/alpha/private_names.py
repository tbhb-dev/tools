"""Flag private repository names in a public pull request draft.

`private_name_problems` checks whole tokens, ignoring case and a trailing
`.git`. A `-private` component or a configured name is private. The exact,
case-sensitive token `example-private` is a reviewed placeholder exemption.

`draft_problems` combines `[pr_check] private_repos` with the required list in
`~/.local/state/tbhb/private-repos.txt`. Its header can restrict words to path
positions. Private targets skip the name check. A public target with no usable
host list reports a problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from tbhbdev import userconfig
from tbhbdev.alpha.repo import git

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tbhbdev.alpha.pull_request import Problem

SUFFIX = "-private"
PLACEHOLDER = "example-private"
PRIVATE_COMPONENT = re.compile(r"-private(?:-|$)")
PATH_TOKEN = re.compile(r"(?<=[\w.-])/([\w-]+(?:\.[\w-]+)*)(?![\w-])", re.ASCII)
PATH_HEADER = "# Bare-word exempt, flagged only in path position:"


@dataclass
class Names:
    """Private names and words restricted to repository path positions."""

    private: list[str]
    path_only: set[str]


class NameListError(Exception):
    """The host list cannot supply the required private names."""


REPOSITORY_URL = re.compile(
    r"(?:(?:https?|ssh|git|git\+ssh)://[^/\s]+/[^/\s]+(?:/[^/\s]+)*"
    r"|[^/:\s]+:[^/\s][^\s]*|/?(?:[^/:\s]+/)+[^/\s]+)"
)
MESSAGE = "'{}' names a private repository; describe it generically instead"
TOKEN = re.compile(r"(?<![\w.-])[\w-]+(?:\.[\w-]+)*(?![\w-])", re.ASCII)


def private_name_problems(
    lines: Sequence[str],
    names: Sequence[str],
    first: int = 1,
    *,
    path_only: set[str] | None = None,
) -> list[Problem]:
    """One problem per distinct private-looking token on each line."""
    restricted = path_only or set()
    wanted = {name.lower() for name in names}
    return [
        (number, MESSAGE.format(token))
        for number, line in enumerate(lines, first)
        for token in dict.fromkeys(TOKEN.findall(line))
        if token != PLACEHOLDER
        and (
            _path_private(token, line, restricted)
            or (token.lower() not in restricted and _is_private(token, wanted))
        )
    ]


def _is_private(token: str, wanted: set[str]) -> bool:
    """Whether TOKEN, minus a trailing `.git`, is a private name."""
    name = token.lower().removesuffix(".git")
    return (
        bool(PRIVATE_COMPONENT.search(name)) and not name.startswith("-")
    ) or name in wanted


def _path_private(token: str, line: str, restricted: set[str]) -> bool:
    """Whether a header word appears as org/word or word.git."""
    return token.lower().removesuffix(".git") in restricted and (
        token.lower().endswith(".git") or token in PATH_TOKEN.findall(line)
    )


def _host_names() -> Names:
    """Read the required host list without including its contents in errors."""
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    message = f"private names: cannot read a nonempty list from {path}"
    try:
        lines = path.read_text().splitlines()
    except OSError, UnicodeDecodeError:
        raise NameListError(message) from None
    names = parse_list(lines)
    if not names.private:
        raise NameListError(message)
    return names


def parse_list(lines: Sequence[str]) -> Names:
    """Split host-list lines into private names and path-only words."""
    names: list[str] = []
    path_only: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if line.startswith(PATH_HEADER):
            path_only.update(line.removeprefix(PATH_HEADER).lower().split())
        elif line and not line.startswith("#"):
            names.append(line)
    return Names(names, path_only)


LITERAL_END = re.compile(r"""["']\s*[,+]?\s*$""")
LITERAL_START = re.compile(r"""^\+\s*[rbfRBF]{0,2}["']""")


def joined_literals(lines: Sequence[str]) -> list[tuple[int, str]]:
    """Merge added lines whose adjacent string literals concatenate.

    Each entry is the line number of the first line and the merged text, so a
    name split across literals shows up whole. Only added diff lines merge.
    """
    merged: list[tuple[int, str]] = []
    current: tuple[int, str] | None = None
    for number, line in enumerate(lines, 1):
        if (
            current is not None
            and current[1].startswith("+")
            and LITERAL_START.match(line)
            and LITERAL_END.search(current[1])
        ):
            head = LITERAL_END.sub("", current[1])
            tail = LITERAL_START.sub("", line)
            current = (current[0], head + tail)
        else:
            if current is not None and current[1] != lines[current[0] - 1]:
                merged.append(current)
            current = (number, line)
    if current is not None and current[1] != lines[current[0] - 1]:
        merged.append(current)
    return merged


def _configured() -> list[str]:
    """The configured private names; none when the config can't supply them."""
    try:
        return userconfig.optional_strings("pr_check", "private_repos") or []
    except userconfig.ConfigError, OSError, UnicodeDecodeError:
        return []


def repository_name(url: str) -> str | None:
    """The final repository component of a usable git URL or local path."""
    value = url.strip().rstrip("/")
    try:
        urlsplit(value)
    except ValueError:
        return None
    if REPOSITORY_URL.fullmatch(value) is None:
        return None
    return re.split(r"[/:]", value)[-1].removesuffix(".git").lower() or None


def names_for_repository(name: str | None) -> Names | None:
    """Combined rules, or None for a private target; require the host list."""
    configured = _configured()
    if name is not None and (
        name.endswith(SUFFIX) or name in {item.lower() for item in configured}
    ):
        return None
    host = _host_names()
    if name in {item.lower() for item in host.private} | host.path_only:
        return None
    return Names(configured + host.private, host.path_only)


def private_names() -> Names | None:
    """Names to flag for origin; an unreadable origin counts as public."""
    code, url = git("config", "--get", "remote.origin.url")
    return names_for_repository(repository_name(url) if code == 0 else None)


def draft_problems(lines: Sequence[str], first: int) -> list[Problem]:
    """The private-name problems of a rendered draft, if the check is on."""
    try:
        names = private_names()
    except NameListError as exc:
        return [(first, str(exc))]
    return (
        []
        if names is None
        else private_name_problems(
            lines, names.private, first, path_only=names.path_only
        )
    )
