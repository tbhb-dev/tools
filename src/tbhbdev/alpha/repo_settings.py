"""The repository settings ``alpha repo bootstrap`` applies: pure functions.

Nothing here reads a file, runs a program or prints. The config text comes in
as a string, GitHub's reply as a mapping, and each function returns a value:
the validated config, the settings one repository should have, the keys that
differ from what it has, and the text and log record that describe them.

Every key is a field of GitHub's "update a repository" endpoint, and only the
keys in ``KEYS`` are ever accepted, so no config can rename, archive, hide or
expose a repository, or reach a security setting.
"""

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

type Settings = dict[str, object]
type Changes = dict[str, tuple[object, object]]

CONFIG_FILE = "repo-settings.toml"
LOG_VERSION = 1

TITLE = "squash_merge_commit_title"
MESSAGE = "squash_merge_commit_message"
# The values GitHub documents for each commit-message setting.
ENUMS: dict[str, tuple[str, ...]] = {
    TITLE: ("PR_TITLE", "COMMIT_OR_PR_TITLE"),
    MESSAGE: ("PR_BODY", "COMMIT_MESSAGES", "BLANK"),
}
# The allowlist, in the order the commands list a repository's keys. Each key
# outside ENUMS takes true or false.
KEYS = (
    "allow_squash_merge",
    "allow_merge_commit",
    "allow_rebase_merge",
    TITLE,
    MESSAGE,
    "delete_branch_on_merge",
    "has_issues",
    "has_wiki",
    "has_projects",
    "allow_auto_merge",
    "allow_update_branch",
    "web_commit_signoff_required",
)
# GitHub refuses a message without its title.
COMMIT_PAIR = (TITLE, MESSAGE)

TABLES = ("defaults", "repos")
REPO = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")


class ConfigError(Exception):
    """The config text isn't valid; the message names the file and the key."""


@dataclass(frozen=True)
class Config:
    """The ``[defaults]`` table and each ``[repos."owner/name"]`` table.

    The repository names are lowercase, since GitHub ignores case in them.
    """

    defaults: Settings
    repos: dict[str, Settings]


def valid_repo(name: str) -> bool:
    """Whether NAME is ``owner/name`` with characters GitHub allows."""
    return REPO.match(name) is not None


def check_table(table: object, where: str) -> Settings:
    """The settings in TABLE, each checked against the allowlist.

    Raises:
        ConfigError: TABLE isn't a table, or holds a key that isn't allowed or
            a value of the wrong type.
    """
    if not isinstance(table, dict):
        message = f"{CONFIG_FILE}: [{where}] must be a table"
        raise ConfigError(message)
    entries: dict[str, object] = table
    checked: Settings = {}
    for key, value in entries.items():
        label = f"{CONFIG_FILE}: [{where}] {key}"
        if key not in KEYS:
            allowed = ", ".join(KEYS)
            message = (
                f"{label} is not an allowed setting; the allowed ones are {allowed}"
            )
            raise ConfigError(message)
        if key in ENUMS and value not in ENUMS[key]:
            message = f"{label} must be one of {', '.join(ENUMS[key])}"
            raise ConfigError(message)
        if key not in ENUMS and not isinstance(value, bool):
            message = f"{label} must be true or false"
            raise ConfigError(message)
        checked[key] = value
    return checked


def parse_config(text: str) -> Config:
    """The validated config in TEXT.

    Raises:
        ConfigError: TEXT isn't TOML, lacks ``[defaults]``, holds a table or a
            key that isn't allowed, or names a repository that isn't
            ``owner/name``.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        message = f"{CONFIG_FILE}: {exc}"
        raise ConfigError(message) from exc
    for key in data:
        if key not in TABLES:
            message = f"{CONFIG_FILE}: {key} is not a table this command reads"
            raise ConfigError(message)
    if "defaults" not in data:
        message = f"{CONFIG_FILE}: [defaults] is missing"
        raise ConfigError(message)
    defaults = check_table(data["defaults"], "defaults")
    listed = data.get("repos", {})
    if not isinstance(listed, dict):
        message = f"{CONFIG_FILE}: [repos] must be a table"
        raise ConfigError(message)
    repos: dict[str, Settings] = {}
    tables: dict[str, object] = listed
    for name, table in tables.items():
        if not valid_repo(name):
            message = f'{CONFIG_FILE}: [repos."{name}"] must be named owner/name'
            raise ConfigError(message)
        if name.lower() in repos:
            message = f'{CONFIG_FILE}: [repos."{name}"] repeats a repository'
            raise ConfigError(message)
        repos[name.lower()] = check_table(table, f'repos."{name}"')
    return Config(defaults, repos)


def desired(config: Config, repo: str) -> Settings:
    """The settings REPO should have: the defaults, with its own table on top."""
    return {**config.defaults, **config.repos.get(repo.lower(), {})}


def current(payload: Mapping[str, object]) -> Settings:
    """The allowlisted keys of GitHub's reply, and nothing else of it."""
    return {key: payload[key] for key in KEYS if key in payload}


def diff(have: Mapping[str, object], want: Mapping[str, object]) -> Changes:
    """Each wanted key whose value differs, as ``key: (before, after)``.

    A key the reply lacks counts as differing, with a before of None.
    """
    return {
        key: (have.get(key), want[key])
        for key in KEYS
        if key in want and have.get(key) != want[key]
    }


def patch_body(changes: Changes, want: Mapping[str, object]) -> Settings:
    """The JSON for one update: the changed keys only.

    GitHub requires the squash title with the squash message, so when either
    changes, the body carries both of the wanted values.
    """
    body: Settings = {key: after for key, (_, after) in changes.items()}
    if body.keys() & set(COMMIT_PAIR):
        body |= {key: want[key] for key in COMMIT_PAIR if key in want}
    return body


def show(value: object) -> str:
    """A setting's value as JSON: true, "PR_TITLE" or null."""
    return json.dumps(value)


def render_changes(changes: Changes) -> str:
    """One ``key: before -> after`` line for each change, then the count."""
    lines = [
        f"{key}: {show(before)} -> {show(after)}"
        for key, (before, after) in changes.items()
    ]
    count = len(changes)
    lines.append(f"{count} {'change' if count == 1 else 'changes'}")
    return "\n".join(lines)


def render_create(repo: str, visibility: str, want: Mapping[str, object]) -> str:
    """What a dry run says it would create, and the settings it would then apply."""
    lines = [f"would create {repo} ({visibility})", "settings it would get:"]
    lines += [f"{key}: {show(want[key])}" for key in KEYS if key in want]
    return "\n".join(lines)


def timestamp(moment: datetime) -> str:
    """MOMENT in UTC, as RFC 3339 to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_entry(
    repo: str, changes: Changes, moment: datetime, *, created: bool, verified: bool
) -> Settings:
    """The record of one run: the repository, what changed and whether it held."""
    return {
        "v": LOG_VERSION,
        "ts": timestamp(moment),
        "repo": repo,
        "created": created,
        "changed": {key: [before, after] for key, (before, after) in changes.items()},
        "verified": verified,
    }
