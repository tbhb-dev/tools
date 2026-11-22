"""Flag private repository names in a public pull request draft.

`private_name_problems` checks whole tokens, ignoring case and a trailing
`.git`. A `-private` component or a configured name is private. The exact,
case-sensitive token `example-private` is a reviewed placeholder exemption.
Public rule IDs are exempt unless configured as private names. Exact public
URL prefixes listed in `~/.local/state/tbhb/public-url-prefixes.txt` are removed
from a line before it is checked, so nothing else in a URL is exempt.

`draft_problems` combines `[pr_check] private_repos` with the required list in
`~/.local/state/tbhb/private-repos.txt`. Its header can restrict words to path
positions. Private targets skip the name check. A public target with no usable
host list reports a problem.
"""

from __future__ import annotations

import re
import sys
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
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
PREFIX_FILE_LIMIT = 64 * 1024
PREFIX_LINE_LIMIT = 512
PROSE = "the quick brown fox"
UNSAFE_WILDCARD = re.compile(r"(?:/|\(|\|)\.[*+]")
NAME_CHAR = re.compile(r"[A-Za-z0-9._-]")
PATH_HEADER = "# Bare-word exempt, flagged only in path position:"


@dataclass
class Names:
    """Private names and words restricted to repository path positions."""

    private: list[str]
    path_only: set[str]
    prefixes: tuple[re.Pattern[str], ...] = ()


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
    prefixes: Sequence[re.Pattern[str]] = (),
) -> list[Problem]:
    """One problem per distinct private-looking token on each line."""
    restricted = path_only or set()
    wanted = {name.lower() for name in names}
    return [
        (number, MESSAGE.format(token))
        for number, line in enumerate(lines, first)
        for token in dict.fromkeys(
            TOKEN.findall(_without_prefixes(line, prefixes, wanted | restricted))
        )
        if token != PLACEHOLDER
        and (token.lower() not in PUBLIC_RULE_IDS or token.lower() in wanted)
        and (
            _path_private(token, line, restricted)
            or (token.lower() not in restricted and _is_private(token, wanted))
        )
    ]


def _without_prefixes(
    line: str, prefixes: Sequence[re.Pattern[str]], wanted: set[str] | None = None
) -> str:
    """LINE with each exempt public URL prefix span replaced by a space.

    A span is removed only if it ends at a name boundary and holds no private
    name that the prefix does not itself spell out.
    """
    names = wanted or set()
    for prefix in prefixes:
        literal = set(TOKEN.findall(prefix.pattern.replace("\\", "").lower()))
        line = prefix.sub(partial(_span, literal=literal, wanted=names), line)
    return line


def _span(match: re.Match[str], *, literal: set[str], wanted: set[str]) -> str:
    """A space if MATCH may be exempt, else its text unchanged."""
    text = match.group()
    after = match.string[match.end() : match.end() + 1]
    if not text.endswith("/") and after and re.match(NAME_CHAR, after):
        return text
    if any(
        _is_private(token, wanted) and token.lower() not in literal
        for token in TOKEN.findall(text)
    ):
        return text
    return " "


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


def public_url_prefixes() -> tuple[re.Pattern[str], ...]:
    """Exact public URL prefixes from the host file, one regex per line.

    A file problem (missing, unreadable, NUL byte, over the size cap) means no
    prefixes. A line that is too long, does not start with `https://`, starts
    a path segment with a `.*` or `.+` wildcard, does not compile, or matches
    the empty string or ordinary prose is skipped with a warning naming only
    its line number, so a fault exempts nothing.
    """
    path = Path.home() / ".local/state/tbhb/public-url-prefixes.txt"
    try:
        with path.open("rb") as handle:
            data = handle.read(PREFIX_FILE_LIMIT + 1)
        if len(data) > PREFIX_FILE_LIMIT or b"\0" in data:
            return ()
        lines = data.decode().splitlines()
    except OSError, UnicodeDecodeError:
        return ()
    found: list[re.Pattern[str]] = []
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = _prefix_pattern(line)
        if pattern is None:
            sys.stderr.write(f"public-url-prefixes: line {number} skipped\n")
        else:
            found.append(pattern)
    return tuple(found)


def _prefix_pattern(line: str) -> re.Pattern[str] | None:
    """The compiled prefix for LINE, or None if it is unsafe."""
    if (
        len(line) > PREFIX_LINE_LIMIT
        or not line.startswith("https://")
        or UNSAFE_WILDCARD.search(line)
    ):
        return None
    try:
        pattern = re.compile(rf"(?<![\w./-]){line}", re.ASCII)
        if pattern.search("") or pattern.search(PROSE):
            return None
    except re.error, OverflowError, RecursionError:
        return None
    return pattern


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
    return Names(names, path_only, public_url_prefixes())


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
    return Names(configured + host.private, host.path_only, host.prefixes)


def private_names() -> Names | None:
    """Names to flag for origin; an unreadable origin counts as public."""
    code, url = git("remote", "get-url", "--push", "origin")
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
            lines,
            names.private,
            first,
            path_only=names.path_only,
            prefixes=names.prefixes,
        )
    )
