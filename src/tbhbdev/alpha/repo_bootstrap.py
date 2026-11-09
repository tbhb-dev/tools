"""``tbhbdev alpha repo bootstrap``: apply the standard settings to a repository.

The settings come from the ``repo-settings.toml`` that an installed plugin
supplies, so they change only by a pull request in that plugin's repository.
This module reads the config, calls ``gh``, appends the log and prints; every
decision about the settings is a pure function in ``repo_settings``.

The only calls it makes are ``GET`` and ``PATCH`` on ``repos/OWNER/REPO`` and
``POST`` on ``orgs/ORG/repos``, all through ``gh api`` with ``GH_CONFIG_DIR``
set to the agent's gh config. It calls no rulesets, branch protection,
collaborator, team or webhook endpoint, and puts no token on a command line.
"""

import argparse
import json
import os
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev import gh_agent, plugins
from tbhbdev.alpha import repo_settings as settings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

LOG_NAME = "repo-bootstrap.jsonl"
VISIBILITIES = ("private", "public")


class GhError(Exception):
    """A gh call failed, or gave a reply this command can't use."""


class NotFoundError(GhError):
    """A GET came back 404."""


def gh_env(env: Mapping[str, str], home: Path) -> dict[str, str]:
    """ENV with ``GH_CONFIG_DIR`` pointing at the agent's gh config in HOME."""
    return {**env, "GH_CONFIG_DIR": str(home / gh_agent.CONFIG_DIR)}


def gh_api(method: str, path: str, body: Mapping[str, object] | None = None) -> object:
    """The JSON reply of ``gh api --method METHOD PATH``, sending BODY on stdin.

    Raises:
        NotFoundError: gh exits 1 and says HTTP 404.
        GhError: gh can't run, fails for any other reason, or replies with
            something that isn't JSON.
    """
    argv = ["gh", "api", "--method", method, path]
    if body is not None:
        argv += ["--input", "-"]
    try:
        done = subprocess.run(  # nosec B603
            argv,
            input="" if body is None else json.dumps(body),
            capture_output=True,
            text=True,
            check=False,
            env=gh_env(os.environ, Path.home()),
        )
    except OSError as exc:
        message = f"cannot run gh: {exc}"
        raise GhError(message) from exc
    if done.returncode != 0:
        why = " ".join(done.stderr.split())
        message = f"gh api {method} {path} failed (exit {done.returncode}): {why}"
        if done.returncode == 1 and "HTTP 404" in done.stderr:
            raise NotFoundError(message)
        raise GhError(message)
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        message = f"gh api {method} {path} replied with something that isn't JSON"
        raise GhError(message) from exc


def read_repo(repo: str) -> dict[str, object]:
    """GitHub's record of REPO.

    Raises:
        NotFoundError: REPO doesn't exist, or the agent can't see it.
        GhError: gh fails, or the reply isn't an object.
    """
    payload = gh_api("GET", f"repos/{repo}")
    if not isinstance(payload, dict):
        message = f"gh api GET repos/{repo} replied with something that isn't an object"
        raise GhError(message)
    return payload


def load_config() -> settings.Config:
    """The config a plugin supplies, validated.

    Raises:
        settings.ConfigError: No plugin supplies it, or it isn't valid.
        plugins.PluginError: More than one plugin supplies it.
    """
    text = plugins.supplied(settings.CONFIG_FILE)
    if text is None:
        message = f"no installed plugin supplies the config file {settings.CONFIG_FILE}"
        raise settings.ConfigError(message)
    return settings.parse_config(text)


def log_path(env: Mapping[str, str]) -> Path:
    """The log: ``tbhbdev/repo-bootstrap.jsonl`` under the XDG state directory."""
    state = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "tbhbdev" / LOG_NAME


def append_log(entry: Mapping[str, object]) -> None:
    """Append ENTRY as one JSON line, creating the directory 0700 and file 0600."""
    path = log_path(os.environ)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(entry) + "\n")


def now() -> datetime:
    """The current time."""
    return datetime.now(UTC)


def record(
    repo: str, changes: settings.Changes, *, created: bool, verified: bool
) -> None:
    """Append the log line for a run that changed something."""
    append_log(
        settings.log_entry(repo, changes, now(), created=created, verified=verified)
    )


def create_repo(repo: str, visibility: str) -> None:
    """Create REPO under its organization as the agent."""
    org, _, name = repo.partition("/")
    gh_api("POST", f"orgs/{org}/repos", {"name": name, "visibility": visibility})


def apply_changes(
    repo: str,
    changes: settings.Changes,
    want: settings.Settings,
    *,
    created: bool,
) -> None:
    """Send the update, read the repository again and log the run.

    The log line is written once the update has gone out, even when the read
    shows it didn't hold, and for a created repository even when the update
    fails, since both left something behind.

    Raises:
        GhError: The update fails, or a key still differs after it.
    """
    try:
        if changes:
            gh_api("PATCH", f"repos/{repo}", settings.patch_body(changes, want))
        left = settings.diff(settings.current(read_repo(repo)), want)
    except GhError:
        if created:
            record(repo, changes, created=True, verified=False)
        raise
    record(repo, changes, created=created, verified=not left)
    if left:
        message = f"{repo}: still differs after the update: {', '.join(left)}"
        raise GhError(message)


def bootstrap(
    repo: str, config: settings.Config, *, dry_run: bool, visibility: str | None
) -> int:
    """Bring REPO to its settings, creating it first when VISIBILITY is given.

    Raises:
        GhError: A gh call fails, REPO is missing and VISIBILITY isn't given,
            its visibility isn't the one asked for, or an update doesn't hold.
    """
    want = settings.desired(config, repo)
    created = False
    try:
        payload = read_repo(repo)
    except NotFoundError as exc:
        if visibility is None:
            message = (
                f"{repo} does not exist; to create it, add --create with "
                "--private or --public"
            )
            raise GhError(message) from exc
        if dry_run:
            print(settings.render_create(repo, visibility, want))
            return 0
        create_repo(repo, visibility)
        created = True
        payload = read_repo(repo)
    else:
        if visibility is not None and payload.get("visibility") != visibility:
            message = (
                f"{repo} exists and is {payload.get('visibility')}, not "
                f"{visibility}; nothing changed"
            )
            raise GhError(message)
    changes = settings.diff(settings.current(payload), want)
    if dry_run:
        print(settings.render_changes(changes))
        return 0
    if not changes and not created:
        print(f"{repo}: up to date")
        return 0
    apply_changes(repo, changes, want, created=created)
    print(f"{repo}: created" if created else f"{repo}: updated")
    print(settings.render_changes(changes))
    return 0


def repo_name(text: str) -> str:
    """TEXT as ``ORG/REPO``.

    Raises:
        argparse.ArgumentTypeError: TEXT isn't a repository name.
    """
    if not settings.valid_repo(text):
        message = "must be ORG/REPO"
        raise argparse.ArgumentTypeError(message)
    return text


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(prog=prog, description="Repository tools.")
    sub = parser.add_subparsers(dest="verb", required=True)
    boot = sub.add_parser("bootstrap", help="apply the standard repository settings")
    boot.add_argument("repo", metavar="ORG/REPO", type=repo_name)
    boot.add_argument("--dry-run", action="store_true")
    boot.add_argument("--create", action="store_true")
    kind = boot.add_mutually_exclusive_group()
    for name in VISIBILITIES:
        kind.add_argument(
            f"--{name}", dest="visibility", action="store_const", const=name
        )
    return parser


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    """Run ``bootstrap`` as the arguments say; the exit status."""
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    if args.create and args.visibility is None:
        parser.error("--create requires --private or --public")
    if args.visibility is not None and not args.create:
        parser.error("--private and --public require --create")
    try:
        return bootstrap(
            args.repo,
            load_config(),
            dry_run=args.dry_run,
            visibility=args.visibility,
        )
    except (settings.ConfigError, plugins.PluginError, GhError, OSError) as exc:
        print(f"{prog}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
