"""Flag private repository names in a public pull request draft.

`private_name_problems` checks whole tokens, ignoring case and a trailing
`.git`. A `-private` component or a configured name is private. The exact,
case-sensitive token `example-private` is a reviewed placeholder exemption.
Public rule IDs are exempt unless configured as private names. A token that is a
segment of the file path of a parsed file view URL (`/owner/repo/blob/ref/...`) is
exempt, because it names something in that repository. It stays flagged when the
URL's earlier segments include the organization, and in a query, a fragment or
after the URL's end. The organization followed by `/`, `:` or `%2f` and a name is
always flagged.

`draft_problems` combines `[pr_check] private_repos` with the required list in
`~/.local/state/tbhb/private-repos.txt`. Its header can restrict words to path
positions. Private targets skip the name check. A public target with no usable
host list reports a problem.
"""

from __future__ import annotations

import re
from contextlib import suppress
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
# Upstream gitleaks rule IDs, plus the canary already public on main.
PUBLIC_RULE_IDS = frozenset(
    {
        "mailgun-private-api-token",
        "shopify-private-app-access-token",
        "tbhb-private-canary",
    }
)
PRIVATE_COMPONENT = re.compile(r"-private(?:-|$)")
PATH_TOKEN = re.compile(r"(?<=[\w.-])/([\w-]+(?:\.[\w-]+)*)(?![\w-])", re.ASCII)
# A URL's host and path. The path ends at whitespace, a closing delimiter, a comma,
# a query or a fragment, so punctuation after a URL never extends it.
URL_PATH = re.compile(
    r"(?<![\w./-])(?:[a-z][a-z0-9+.-]*://)?[\w-]+(?:\.[\w-]+)+(?::\d+)?"
    r"(?P<path>/[^\s)>\]\"'`,?#]*)",
    re.ASCII | re.IGNORECASE,
)
# A file view URL is /owner/repo/<view>/ref/file path; only its file path segments
# name things in that repository.
FILE_VIEWS = frozenset({"blob", "tree", "raw", "blame"})
ORG = "tbhb-dev"
# Segments before the file path: owner, repository, view, ref.
FILE_START = 4
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
        for token in dict.fromkeys(_outside_file_paths(line))
        if token != PLACEHOLDER
        and (token.lower() not in PUBLIC_RULE_IDS or token.lower() in wanted)
        and (
            _path_private(token, line, restricted)
            or (token.lower() not in restricted and _is_private(token, wanted))
        )
    ]


def _outside_file_paths(line: str) -> list[str]:
    """The tokens of LINE, minus those in a file view URL's file path."""
    line = re.sub(r"%2f", "/", line, flags=re.IGNORECASE)
    inside = [span for match in URL_PATH.finditer(line) for span in _file_spans(match)]
    return [
        match.group()
        for match in TOKEN.finditer(line)
        if not any(start <= match.start() < end for start, end in inside)
    ]


def _file_spans(match: re.Match[str]) -> list[tuple[int, int]]:
    """The spans of the file path segments of a file view URL's path."""
    segments = list(re.finditer(r"[^/]+", match["path"]))
    names = [segment.group().lower() for segment in segments]
    if len(names) <= FILE_START or names[2] not in FILE_VIEWS or ORG in names:
        return []
    base = match.start("path")
    return [(base + seg.start(), base + seg.end()) for seg in segments[FILE_START:]]


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
    names: list[str] = []
    path_only: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if line.startswith(PATH_HEADER):
            path_only.update(line.removeprefix(PATH_HEADER).lower().split())
        elif line and not line.startswith("#"):
            names.append(line)
    if not names:
        raise NameListError(message)
    return Names(names, path_only)


def _configured(*, strict: bool = False) -> list[str]:
    """Configured names; optionally refuse a config that cannot supply them."""
    try:
        if strict:
            with suppress(FileNotFoundError):
                userconfig.path().stat()
        return userconfig.optional_strings("pr_check", "private_repos") or []
    except userconfig.ConfigError, OSError, UnicodeDecodeError:
        if strict:
            message = "cannot read supplemental private-name list"
            raise NameListError(message) from None
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
