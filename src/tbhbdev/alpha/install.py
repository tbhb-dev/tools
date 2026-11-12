"""`tbhbdev alpha install`: the git hooks and the user-level harness hooks.

Run in a clone of a tbhb-dev repository, it installs that clone's git hooks
into `$(git rev-parse --git-common-dir)/hooks`, which every worktree of the
clone shares. The pre-push guard refuses rewritten history and private names
before prek, replaying identical arguments and stdin bytes to both. Public
pushes require a readable, nonempty `~/.local/state/tbhb/private-repos.txt`,
combined with `[pr_check] private_repos`. The list's header defines words
flagged only in path positions. Nonempty SKIP or PREK_SKIP refuses a push.
Without tbhbdev on PATH, it refuses pushes targeting a tbhb-dev repository,
using the hook URL argument or the named remote's URL. prek also runs
pre-commit. The agent identity hooks
(prepare-commit-msg, commit-msg, post-commit) are plain shims that run
`tbhbdev alpha identity`: prek stashes unstaged changes around every stage
and fails a hook whenever the work tree changes while it runs, so a
concurrent agent's edit during a commit would fail the commit or be
reverted. When the prek config also has hooks for one of those stages, prek's
own shim for it is installed as `<hook>.prek` and the identity shim runs it
afterwards. Both always run, and either failing fails the commit. Without
tbhbdev on PATH, the identity part of a shim fails the commit when origin
is a tbhb-dev repository, by repo.py's own pattern, and does nothing
elsewhere.

core.hooksPath set means prek won't install, so that case warns and leaves the
hooks alone. A foreign hook stays in place. A foreign pre-push hook fails the
install and must be moved aside before retrying. Session starts can overlap,
so a lock directory in the hooks directory holds its owner's PID and lets one
run at a time touch the hooks. A run waits up to TBHB_INSTALL_LOCK_TIMEOUT
seconds (default 15) and breaks a lock whose owner is no longer running. The
lock also coordinates with older installers.

Unless `--hooks-only` is set, it then merges the harness hooks into each
harness's user-level config:

    claude       settings.json in $CLAUDE_CONFIG_DIR, default .claude in HOME
    codex        hooks.json in $CODEX_HOME, default .codex in HOME
    antigravity  .gemini/config/hooks.json in HOME

The paths and hook formats come from each harness's documentation:
https://code.claude.com/docs/en/settings and
https://code.claude.com/docs/en/hooks for Claude Code,
https://learn.chatgpt.com/docs/hooks for Codex ("Where Codex looks for
hooks"; Codex runs a hook only when `config.toml` trusts its hash), and
https://antigravity.google/docs/hooks?tab=cli for Antigravity ("hooks.json
File Locations").

Codex runs a hook only when `config.toml` holds a trusted hash for it, so
after the Codex merge this command sets `[hooks.state."<key>"].trusted_hash`
there for each Codex hook it wrote, and for no other hook (see codex_trust). The
file is backed up and replaced like the other configs, a new one is private
(mode 0600), an invalid one is reported and left alone, and a run that changes
nothing writes nothing. Each Codex hook has an explicit timeout, so the hash
covers what is on disk. config.toml gets two edits in one run, trust first and
then the environment (below), each reading the file the other left, so neither
drops the other's keys.

Claude Code also gets two hooks with optional `[harness]` settings in the
tbhbdev user config (see tbhbdev.userconfig): `alpha state-guard`, on
PreToolUse for Bash, SendMessage, Write and Edit, on Stop and on SessionStart,
and `alpha session-context`, on SessionStart. Both run with a 10 second timeout.
Sign-off and claim checks run without config. Only checks that need a setting
are silent when it is absent. Both hooks run regardless of repository origin.

Every command it writes ends in the shell comment `# tbhbdev alpha install`,
which marks it as this command's. A rerun replaces its own entries where they
stand and never removes or reorders anyone else's. A config whose content
would not change is not written. Otherwise the old file is copied to
`<name>.tbhbdev-backup.<UTC time>` and the new one replaces it atomically. A
symlinked config is written through the link. A config that can't be read,
merged or written is reported, and the other harnesses still go ahead.

It also sets the environment both Claude Code and Codex give their
commands: `SSH_AUTH_SOCK` set to the empty string, merged into the `env`
object of Claude's settings.json and into the `shell_environment_policy.set`
table of Codex's config.toml (`$CODEX_HOME`, default .codex in HOME), so an
agent's command can't reach the 1Password SSH agent and prompt the operator.
Host provisioning sets the variable for the account an agent runs as, but gives
the operator's own account no harness environment, so this command is the only
thing that sets it there. config.toml is edited with tomlkit, which keeps every
other key, comment and the layout, and gets the same backup and up-to-date
rules. And it writes the gh shim (see gh_shim): an executable `gh` in
`$XDG_DATA_HOME/tbhbdev/agent-bin`, left alone when it is already current.
None of this prompts, runs op or needs the 1Password app, so configuration
management can run it unattended, and a rerun changes nothing. It never
writes PATH into a harness config: Claude's SessionStart hook puts the shim
on PATH (below), and Codex and Antigravity get it from `tbhbdev alpha
agent-env`.

`--harness` limits the config merge to one harness. `--dry-run` writes
nothing: it prints each config's diff, with one diff of Codex's `config.toml`
for its trust and environment, and the git hooks it would install.
`--hook` is the SessionStart entry point. When `$CLAUDE_ENV_FILE` is set, it
first appends `export PATH=<agent-bin>":$PATH"` to that file once, which
Claude Code sources before each Bash command. Then it reads the hook payload
on stdin, stops outside a tbhb-dev repository, and installs only the git
hooks of the worktree the payload names. Claude Code adds a SessionStart
hook's stdout to the model's context, so `--hook` sends all its output,
prek's too, to stderr, and it always exits 0, so a broken install never
blocks a session.
"""

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess  # nosec B404
import sys
import time
from contextlib import contextmanager, redirect_stdout, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from tbhbdev import plugins
from tbhbdev.alpha.gh_shim import bin_dir, wrapper_text
from tbhbdev.alpha.identity import codex_home
from tbhbdev.alpha.repo import (
    TBHB_DEV_ORIGIN,
    TBHB_DEV_ORIGIN_ERE,
    git,
    hook_cwd,
    in_tbhb_dev,
    read_hook_stdin,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from typing import IO

type Json = dict[str, object]

# Each `cast` below carries `pragma: no mutate`: it is a runtime no-op, so no test
# could tell a mutated type string from the original.

PROG = "tbhbdev alpha install"
MARK = "# tbhbdev alpha install"
SHIM_MARK = "# tbhbdev alpha agent identity hook (tbhbdev alpha install)"
# The identity shims this command and the old install script write.
SHIM_MARKS = (
    SHIM_MARK,
    "# ascm interim agent identity hook (tools/interim/install.sh)",
)
# Hooks this command may replace: those shims and prek's own.
REPLACEABLE = (*SHIM_MARKS, "File generated by prek")
IDENTITY_HOOKS = (
    ("prepare-commit-msg", "trailer"),
    ("commit-msg", "check"),
    ("post-commit", "consume"),
)
# Names live at module level, where mutmut leaves them alone: git config keys
# are case-insensitive and --get is git config's default action, so a mutant of
# this one would be equivalent.
HOOKS_PATH = ("config", "--get", "core.hooksPath")
LOCK = ".tbhb-install.lock"
PID = "pid"
TIMEOUT_ENV = "TBHB_INSTALL_LOCK_TIMEOUT"
DEFAULT_TIMEOUT = "15"
WHOLE_SECONDS = re.compile(r"[0-9]+")
CLAUDE = "claude"
CODEX = "codex"
ANTIGRAVITY = "antigravity"
HARNESSES = (CLAUDE, CODEX, ANTIGRAVITY)
AGY_EDITS = (
    "write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content"
)
AGY_TIMEOUT = 30
# The environment both harnesses give their commands: no SSH agent, so a
# command can't reach the 1Password SSH agent and prompt the operator.
AGENT_ENVIRONMENT = {"SSH_AUTH_SOCK": ""}
POLICY = "shell_environment_policy"
GH_SHIM_MODE = 0o755
AGY_PR_TIMEOUT = 120
HARNESS_TIMEOUT = 10
# Codex's documented default hook timeout, written out so the trusted hash covers
# exactly what is on disk.
CODEX_TIMEOUT = 600

# Without tbhbdev on PATH, a shim blocks the commit only when origin is a
# tbhb-dev repository, by the same pattern as repo.is_tbhb_dev_url. A newline
# in the origin becomes a space, so grep sees the whole origin as one line.
ORIGIN_GREP = f"^{TBHB_DEV_ORIGIN_ERE}$"
SHIM = """#!/bin/sh
{mark}
if command -v tbhbdev >/dev/null 2>&1; then
  tbhbdev alpha identity {verb} "$@"
  status=$?
elif origin=$(git remote get-url origin 2>/dev/null) &&
  printf '%s' "$origin" | LC_ALL=C tr '\\n' ' ' |
  LC_ALL=C grep -Eqi '{origin}'; then
  echo "{hook}: tbhbdev is not on PATH, so the identity hooks can't run" >&2
  echo "{hook}: install it with uv tool install, and put ~/.local/bin on PATH" >&2
  status=1
else
  status=0
fi
next="$(dirname "$0")/{hook}.prek"
if [ -x "$next" ]; then
  "$next" "$@" || status=$?
fi
exit $status
"""


PRE_PUSH_SHIM = """#!{python}
{mark}
import pathlib
import re
import shutil
import subprocess
import sys

payload = sys.stdin.buffer.read()
status = 0
if shutil.which("tbhbdev"):
    status = subprocess.run(
        ["tbhbdev", "alpha", "pre-push-guard", *sys.argv[1:]],
        input=payload, check=False,
    ).returncode
else:
    target = sys.argv[2]
    if not target:
        remote = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "-c", "diff.external=", "-c", "core.pager=cat",
             "remote", "get-url", "--push", sys.argv[1]],
            capture_output=True, text=True, check=False,
        )
        target = remote.stdout if remote.returncode == 0 else ""
    if re.fullmatch({pattern!r}, target, re.IGNORECASE | re.ASCII):
        print("pre-push: tbhbdev is not on PATH; push refused", file=sys.stderr)
        status = 1
next_hook = pathlib.Path(__file__).with_name("pre-push.prek")
if next_hook.is_file():
    result = subprocess.run([str(next_hook), *sys.argv[1:]], input=payload, check=False)
    status = status or result.returncode
sys.exit(status)
"""


class InstallError(Exception):
    """A failure that stops the install, with the message to print."""


def command(args: str) -> str:
    """A marked harness hook command that runs `tbhbdev alpha ARGS` from PATH."""
    return f"tbhbdev alpha {args} {MARK}"


def handler(args: str, timeout: int | None = None) -> Json:
    """One command hook handler."""
    entry: Json = {"type": "command", "command": command(args)}
    if timeout is not None:
        entry["timeout"] = timeout
    return entry


def group(matcher: str | None, *handlers: Json) -> Json:
    """A matcher group of handlers, for every tool when `matcher` is None."""
    entry: Json = {} if matcher is None else {"matcher": matcher}
    entry["hooks"] = list(handlers)
    return entry


def claude_hooks() -> dict[str, list[Json]]:
    """Claude Code's hooks, the ones the tbhb-dev repositories used to carry."""
    record = handler("identity record claude")
    guard = handler("state-guard", HARNESS_TIMEOUT)
    return {
        "SessionStart": [
            group(
                None,
                handler("install --hook"),
                record,
                guard,
                handler("session-context", HARNESS_TIMEOUT),
            )
        ],
        "SubagentStart": [group(None, record)],
        "PreToolUse": [
            group("Bash|Agent", record),
            group("Bash", handler("guard-gh-pr")),
            group("Bash", handler("guard-op")),
            group("Write|Edit|MultiEdit|NotebookEdit", handler("guard-op")),
            group("Bash", handler("guard-gh-poll")),
            group("Write|Edit|MultiEdit|NotebookEdit|Bash", handler("guard-scripts")),
            group("Bash|SendMessage|Write|Edit", guard),
        ],
        "Stop": [group(None, guard)],
        "PostToolUse": [
            group("Write|Edit|MultiEdit|NotebookEdit|Bash", record),
            group("Write|Edit|MultiEdit", handler("pr hook claude")),
        ],
        "PostToolUseFailure": [group("Bash", record)],
        "PostModelSwitch": [group(None, record)],
    }


def codex_hooks() -> dict[str, list[Json]]:
    """Codex's hooks, the ones the tbhb-dev repositories used to carry."""
    record = handler("identity record codex", CODEX_TIMEOUT)
    return {
        "SessionStart": [group(None, record)],
        "SubagentStart": [group(None, record)],
        "PostToolUse": [
            group("apply_patch", record),
            group("apply_patch", handler("pr hook codex", CODEX_TIMEOUT)),
        ],
        "PreToolUse": [
            group("^Bash$", handler("guard-gh-pr", CODEX_TIMEOUT)),
            group("^Bash$", handler("guard-gh-poll", CODEX_TIMEOUT)),
            group("^Bash$", handler("guard-scripts", CODEX_TIMEOUT)),
            group("^apply_patch$", handler("guard-scripts", CODEX_TIMEOUT)),
            group("^(Bash|shell)$", handler("guard-op", CODEX_TIMEOUT)),
            group("apply_patch", handler("guard-op", CODEX_TIMEOUT)),
        ],
    }


def agy_hooks() -> Json:
    """Antigravity's named hook groups, the ones the repositories used to carry."""
    record = handler("identity record antigravity", AGY_TIMEOUT)
    return {
        "tbhbdev-alpha-identity": {
            "SessionStart": [handler("identity record antigravity")],
            "PreToolUse": [group("run_command", record)],
            "PostToolUse": [group(AGY_EDITS, record)],
        },
        "tbhbdev-alpha-op-account": {
            "PreToolUse": [
                group("run_command", handler("guard-op", AGY_TIMEOUT)),
                group(AGY_EDITS, handler("guard-op", AGY_TIMEOUT)),
            ],
        },
        "tbhbdev-alpha-pull-request": {
            "PreToolUse": [group("run_command", handler("guard-gh-pr", AGY_TIMEOUT))],
            "PostToolUse": [
                group(AGY_EDITS, handler("pr hook antigravity", AGY_PR_TIMEOUT))
            ],
        },
        "tbhbdev-alpha-gh-poll": {
            "PreToolUse": [group("run_command", handler("guard-gh-poll", AGY_TIMEOUT))],
        },
        "tbhbdev-alpha-scripts": {
            "PreToolUse": [
                group("run_command|write_file", handler("guard-scripts", AGY_TIMEOUT))
            ],
        },
    }


def config_path(harness: str, env: Mapping[str, str]) -> Path:
    """Where a harness reads its user-level hooks."""
    if harness == CLAUDE:
        base = env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        return Path(base) / "settings.json"
    if harness == CODEX:
        return codex_home(env) / "hooks.json"
    return Path.home() / ".gemini" / "config" / "hooks.json"


# ------------------------------------------------------------- config merge


def ours(entry: object) -> bool:
    """Whether a hook handler is one this command wrote."""
    if not isinstance(entry, dict):
        return False
    fields = cast("Json", entry)  # pragma: no mutate
    text = fields.get("command")
    return isinstance(text, str) and text.endswith(MARK)


def strip_ours(groups: list[object]) -> tuple[list[object], int | None]:
    """The groups without this command's handlers, and where its first one was.

    A group left with no handlers goes. Anything else stays as it was.
    """
    kept: list[object] = []
    first: int | None = None
    for entry in groups:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        fields = cast("Json", entry)  # pragma: no mutate
        handlers = fields.get("hooks")
        if not isinstance(handlers, list):
            kept.append(entry)
            continue
        listed = cast("list[object]", handlers)  # pragma: no mutate
        rest = [h for h in listed if not ours(h)]
        if len(rest) == len(listed):
            kept.append(entry)
            continue
        if first is None:
            first = len(kept)
        if rest:
            kept.append({**fields, "hooks": rest})
    return kept, first


def merge_events(config: Json, desired: Mapping[str, list[Json]]) -> Json:
    """Claude Code and Codex: `config` with its `hooks` events merged.

    This command's groups go where its first old one stood, or at the end.
    """
    current = config.get("hooks", {})
    if not isinstance(current, dict):
        message = "its hooks key is not an object"
        raise InstallError(message)
    hooks = dict(cast("Json", current))  # pragma: no mutate
    for event in [*hooks, *(e for e in desired if e not in hooks)]:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            message = f"its {event} hooks are not a list"
            raise InstallError(message)
        listed = cast("list[object]", groups)  # pragma: no mutate
        kept, first = strip_ours(listed)
        at = len(kept) if first is None else first
        merged = [*kept[:at], *desired.get(event, []), *kept[at:]]
        if merged or first is None:
            hooks[event] = merged
        else:
            del hooks[event]
    return {**config, "hooks": hooks}


def merge_groups(config: Json, desired: Json) -> Json:
    """Antigravity: `config` with this command's named groups set in place."""
    return {**config, **desired}


def merged_config(harness: str, config: Json) -> Json:
    """A harness config with this command's hooks merged in."""
    if harness == CLAUDE:
        env = config.get("env", {})
        if not isinstance(env, dict):
            message = "its env key is not an object"
            raise InstallError(message)
        fields = cast("Json", env)  # pragma: no mutate
        merged = merge_events(config, claude_hooks())
        return {**merged, "env": {**fields, **AGENT_ENVIRONMENT}}
    if harness == CODEX:
        return merge_events(config, codex_hooks())
    return merge_groups(config, agy_hooks())


def read_text(path: Path) -> str | None:
    """A config's text, None when it is missing."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        message = f"it cannot be read ({exc})"
        raise InstallError(message) from exc


def read_config(path: Path) -> tuple[str | None, Json]:
    """A config's text (None when missing) and its object.

    An empty or whitespace-only config is the empty object.
    """
    text = read_text(path)
    if text is None:
        return None, {}
    if not text.strip():
        return text, {}
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        message = f"it is not valid JSON ({exc})"
        raise InstallError(message) from exc
    if not isinstance(loaded, dict):
        message = "it is not a JSON object"
        raise InstallError(message)
    config = cast("Json", loaded)  # pragma: no mutate
    return text, config


def stamp() -> str:
    """The current UTC time, for backup names."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def free_name(path: Path) -> Path:
    """`path`, or `path.N` for the first N that names nothing yet."""
    found, n = path, 1
    while found.exists() or found.is_symlink():
        found = path.with_name(f"{path.name}.{n}")
        n += 1
    return found


def copy_backup(target: Path, backup: Path, mode: int | None) -> None:
    """Copy TARGET to BACKUP with its mode, or with MODE from the first byte."""
    if mode is None:
        shutil.copy2(target, backup)
        return
    backup.touch(mode=mode)
    shutil.copyfile(target, backup)


def open_tmp(tmp: Path, mode: int | None) -> IO[str]:
    """Open the temporary file for writing, created with MODE when given."""
    if mode is None:
        return tmp.open("w")
    return os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w")


def write_config(
    path: Path, old: str | None, new: str, mode: int | None = None
) -> Path | None:
    """Replace a config atomically, backing up the old one; return the backup.

    The new text is synced to disk before it replaces the old. On any failure
    the temporary file goes, and the OSError becomes an InstallError. A MODE
    sets the permissions of the new file and the backup from creation on;
    without one, both keep the old file's.
    """
    target = path.resolve()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    backup = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if old is not None:
            name = f"{target.name}.tbhbdev-backup.{stamp()}"
            backup = free_name(target.with_name(name))
            copy_backup(target, backup, mode)
        with open_tmp(tmp, mode) as out:
            out.write(new)
            out.flush()
            os.fsync(out.fileno())
        if old is not None and mode is None:
            shutil.copymode(target, tmp)
        tmp.replace(target)
    except OSError as exc:
        # The temporary file may never have been made, or its directory may not
        # exist, so whatever unlink says is beside the point.
        with suppress(OSError):
            tmp.unlink()
        raise InstallError(str(exc)) from exc
    return backup


def print_diff(path: Path, old: str | None, new: str) -> None:
    """Print the unified diff from OLD (None when missing) to NEW."""
    before = [] if old is None else old.splitlines(keepends=True)
    diff = difflib.unified_diff(
        before, new.splitlines(keepends=True), str(path), str(path)
    )
    print("".join(diff), end="")


def trust_codex_hooks(
    hooks_path: Path, merged: Json, env: Mapping[str, str], *, dry_run: bool
) -> int:
    """Trust each Codex hook of ours in `config.toml`, and only those.

    Codex runs a hook only when `config.toml` holds its hash. A new file is
    private; an existing one keeps its mode. Any failure leaves it as it was.

    The same write also sets AGENT_ENVIRONMENT, so a run that changes both
    leaves one backup of the user's file, not a second one of the half-edited
    file; install_codex_environment then finds nothing to do. A file whose
    environment can't be merged still gets its trust, and
    install_codex_environment reports the environment.

    codex_trust imports tomlkit, so it is imported here, and the hooks that
    import this module don't load it.
    """
    from tbhbdev.alpha import codex_trust  # noqa: PLC0415 -- see the docstring

    path = codex_home(env) / "config.toml"
    hooks = hooks_path.absolute()
    wanted = codex_trust.our_hashes(hooks, merged, ours)
    try:
        old = read_text(path)
        new = codex_trust.trusted_text(old, hooks, wanted)
        with suppress(InstallError):
            new = with_environment(new)
        if old == new:
            print(f"codex: {len(wanted)} hooks already trusted in {path}")
            return 0
        if dry_run:
            print_diff(path, old, new)
            return 0
        backup = write_config(path, old, new, 0o600 if old is None else None)
    except (InstallError, ValueError) as exc:
        print(f"{PROG}: codex: cannot trust hooks in {path}: {exc}", file=sys.stderr)
        return 1
    saved = f"; the old one is {backup}" if backup else ""
    print(f"codex: trusted {len(wanted)} hooks in {path}{saved}")
    return 0


def install_config(harness: str, env: Mapping[str, str], *, dry_run: bool) -> int:
    """Merge one harness's hooks into its user-level config.

    Codex's hooks are then trusted in its `config.toml`, whether or not
    `hooks.json` changed.
    """
    path = config_path(harness, env)
    try:
        old, config = read_config(path)
        merged = merged_config(harness, config)
    except InstallError as exc:
        print(f"{PROG}: {harness}: cannot merge into {path}: {exc}", file=sys.stderr)
        return 1
    if old is not None and merged == config:
        print(f"{harness}: {path} is up to date")
        status = 0
    else:
        new = json.dumps(merged, indent=2) + "\n"
        status = apply_config(harness, path, old, new, dry_run=dry_run)
    if harness == CODEX and status == 0:
        status = trust_codex_hooks(path, merged, env, dry_run=dry_run)
    return status


def apply_config(
    harness: str, path: Path, old: str | None, new: str, *, dry_run: bool
) -> int:
    """Print the change to a config on a dry run; otherwise write it."""
    if dry_run:
        print_diff(path, old, new)
        return 0
    try:
        backup = write_config(path, old, new)
    except InstallError as exc:
        print(f"{PROG}: {harness}: cannot write {path}: {exc}", file=sys.stderr)
        return 1
    saved = f"; the old one is {backup}" if backup else ""
    print(f"{harness}: wrote {path}{saved}")
    return 0


# ------------------------------------------------------------ private files

# Config names a plugin may supply, and where each goes relative to HOME.
PRIVATE_FILES = (
    ("claude-private.md", Path(".claude") / "CLAUDE.private.md"),
    ("mise-private.toml", Path(".config") / "mise" / "conf.d" / "private.toml"),
)


def install_private_file(name: str, relative: Path, *, dry_run: bool) -> int:
    """Write one file a plugin supplies into HOME; do nothing when none does."""
    path = Path.home() / relative
    try:
        text = plugins.supplied(name)
        if text is None:
            return 0
        old = path.read_text() if path.exists() else None
        if old == text:
            print(f"{name}: {path} is up to date")
            return 0
        if dry_run:
            print_diff(path, old, text)
            return 0
        backup = write_config(path, old, text, 0o600)
    except (plugins.PluginError, InstallError, OSError) as exc:
        print(f"{PROG}: {name}: cannot install {path}: {exc}", file=sys.stderr)
        return 1
    saved = f"; the old one is {backup}" if backup else ""
    print(f"{name}: wrote {path}{saved}")
    return 0


def install_private_files(*, dry_run: bool) -> int:
    """Write each file a plugin supplies into HOME, leaving the rest alone.

    The names come from this list, and the text from the plugin at run time,
    so the package never holds private content.
    """
    status = 0
    for name, relative in PRIVATE_FILES:
        status |= install_private_file(name, relative, dry_run=dry_run)
    return status


# ---------------------------------------------------------- gh environment


def install_gh_shim(*, dry_run: bool) -> int:
    """Write the gh shim (see gh_shim) unless it is already in place."""
    path = bin_dir() / "gh"
    text = wrapper_text()
    try:
        current = path.read_text() if path.is_file() else None
        mode = stat.S_IMODE(path.stat().st_mode) if current is not None else None
        if current == text and mode == GH_SHIM_MODE:
            print(f"gh-shim: {path} is up to date")
            return 0
        if dry_run:
            print(f"gh-shim: would write {path}")
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".gh.{os.getpid()}.tmp")
        tmp.write_text(text)
        tmp.chmod(GH_SHIM_MODE)
        tmp.replace(path)
    except OSError as exc:
        print(f"{PROG}: gh-shim: cannot write {path}: {exc}", file=sys.stderr)
        return 1
    print(f"gh-shim: wrote {path}")
    return 0


def with_environment(old: str | None) -> str:
    """Codex's config.toml text OLD (None when missing) with AGENT_ENVIRONMENT.

    The environment goes in shell_environment_policy.set. tomlkit keeps every
    other key, comment and the file's layout. It is imported here, so the
    hooks that import this module don't load it.
    """
    import tomlkit  # noqa: PLC0415 -- see the docstring
    from tomlkit.items import InlineTable  # noqa: PLC0415

    try:
        document = tomlkit.parse(old or "")
    except ValueError as exc:
        message = f"it is not valid TOML ({exc})"
        raise InstallError(message) from exc
    if POLICY not in document:
        document[POLICY] = tomlkit.table()
    policy = document[POLICY]
    if not isinstance(policy, dict):
        message = f"its {POLICY} is not a table"
        raise InstallError(message)
    if "set" not in policy:
        inline = isinstance(policy, InlineTable)
        policy["set"] = tomlkit.inline_table() if inline else tomlkit.table()
    values: object = policy["set"]
    if not isinstance(values, dict):
        message = f"its {POLICY}.set is not a table"
        raise InstallError(message)
    values.update(AGENT_ENVIRONMENT)
    return document.as_string()


def merged_codex_config(path: Path) -> tuple[str | None, str]:
    """Codex's config.toml text, None when missing, and the merged text."""
    old = read_text(path)
    return old, with_environment(old)


def install_codex_environment(
    env: Mapping[str, str], *, dry_run: bool, in_trust_diff: bool = False
) -> int:
    """Merge AGENT_ENVIRONMENT into Codex's config.toml.

    IN_TRUST_DIFF says trust_codex_hooks succeeded, so a dry run already
    printed this change in its one diff of the file, as a real run writes both
    edits at once; the dry run prints it no second time.
    """
    path = codex_home(env) / "config.toml"
    try:
        old, new = merged_codex_config(path)
    except InstallError as exc:
        print(f"{PROG}: codex: cannot merge into {path}: {exc}", file=sys.stderr)
        return 1
    if old == new:
        print(f"codex: {path} is up to date")
        return 0
    if dry_run and in_trust_diff:
        return 0
    return apply_config(CODEX, path, old, new, dry_run=dry_run)


def export_shim_path(env: Mapping[str, str], *, dry_run: bool) -> None:
    """Claude Code: put the gh shim first on PATH for the session's commands.

    Appends one export line to the SessionStart hook's ``$CLAUDE_ENV_FILE``,
    which Claude Code sources before each Bash command, unless the file
    already has it. Without the variable it does nothing. A file it can't
    read or write is reported on stderr.
    """
    target = env.get("CLAUDE_ENV_FILE")
    if not target:
        return
    path = Path(target)
    line = f'export PATH={shlex.quote(str(bin_dir()))}":$PATH"'
    try:
        text = path.read_text() if path.exists() else ""
        if line in text.splitlines():
            return
        if dry_run:
            print(f"would add to {path}: {line}")
            return
        separator = "\n" if text and not text.endswith("\n") else ""
        with path.open("a") as out:
            out.write(f"{separator}{line}\n")
    except OSError as exc:
        print(f"{PROG}: cannot put the gh shim on PATH: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- git hooks


def lock_timeout(env: Mapping[str, str]) -> int:
    """The lock wait in whole seconds, from TBHB_INSTALL_LOCK_TIMEOUT."""
    raw = env.get(TIMEOUT_ENV, DEFAULT_TIMEOUT)
    if not WHOLE_SECONDS.fullmatch(raw):
        message = f"{TIMEOUT_ENV} must be a whole number of seconds, not {raw!r}"
        raise InstallError(message)
    return int(raw)


def dead_owner(lock: Path) -> int | None:
    """The PID in the lock's pid file when that process isn't running.

    A lock without a readable PID (a run killed between taking the lock and
    writing the file) counts as held, so it waits out the timeout.
    """
    try:
        raw = (lock / PID).read_text().strip()
    except OSError:
        return None
    if not WHOLE_SECONDS.fullmatch(raw):
        return None
    # A PID too long for int() or too large for the OS isn't one any process
    # has, so like a missing PID it counts as held.
    try:
        pid = int(raw)
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    except PermissionError, OverflowError:
        return None
    return None


def try_mkdir(path: Path) -> bool:
    """Make a directory; False when it already exists. mkdir is atomic."""
    try:
        path.mkdir()
    except FileExistsError:
        return False
    return True


def break_stale(lock: Path) -> bool:
    """Remove the lock if its owner is gone; whether this run tried.

    Only the run holding `<lock>.break` may break the lock, and it checks the
    owner again first, so two waiters can't both break it and remove the lock
    the other has just taken.
    """
    breaker = lock.with_name(f"{lock.name}.break")
    if dead_owner(lock) is None or not try_mkdir(breaker):
        return False
    pid = dead_owner(lock)
    if pid is not None:
        (lock / PID).unlink(missing_ok=True)
        with suppress(OSError):
            lock.rmdir()
            print(
                f"{PROG}: removed stale lock {lock}; process {pid} is not running",
                file=sys.stderr,
            )
    breaker.rmdir()
    return True


@contextmanager
def hooks_lock(hooks: Path, timeout: int) -> Generator[None]:
    """Hold the hooks directory's install lock."""
    lock = hooks / LOCK
    tenths = 0
    while not try_mkdir(lock):
        if break_stale(lock):
            continue
        if tenths >= timeout * 10:
            message = (
                f"could not take {lock} within {timeout}s; if no other install "
                "is running, remove it"
            )
            raise InstallError(message)
        time.sleep(0.1)
        tenths += 1
    try:
        (lock / PID).write_text(f"{os.getpid()}\n")
        yield
    finally:
        (lock / PID).unlink(missing_ok=True)
        try:
            lock.rmdir()
        except OSError:
            print(
                f"{PROG}: warning: could not remove {lock}; remove it before the "
                "next run",
                file=sys.stderr,
            )


def find_prek() -> list[str]:
    """How to run prek: from PATH, else through mise."""
    # On a case-insensitive filesystem, as macOS has by default, which("PREK")
    # finds prek too, so the two `which` lines are exempt from mutation.
    if shutil.which("prek"):  # pragma: no mutate
        return ["prek"]
    if shutil.which("mise"):  # pragma: no mutate
        return ["mise", "exec", "--", "prek"]
    message = "prek not found; install it, or install mise"
    raise InstallError(message)


def prek_run(
    prek: list[str], top: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run prek in the work tree, its output passed through."""
    # check=False is subprocess.run's default, so dropping it changes nothing.
    return subprocess.run(  # nosec B603
        [*prek, *args],
        cwd=top,
        check=False,  # pragma: no mutate
        text=True,
        capture_output=True,
    )


def prek_install(prek: list[str], top: Path, *args: str) -> None:
    """Run `prek install ARGS`; stop on failure."""
    done = prek_run(prek, top, "install", *args)
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    if done.returncode != 0:
        message = f"prek install {' '.join(args)} failed"
        raise InstallError(message)


def replaceable(hook: Path) -> bool:
    """Whether a hook is missing or one this command may replace.

    Anything else at its path that isn't a file, such as a directory or a
    dangling symlink, stops the install before it changes the hook.
    """
    if not hook.is_file():
        if hook.exists() or hook.is_symlink():
            message = f"{hook} is not a file; remove it and run the install again"
            raise InstallError(message)
        return True
    text = hook.read_text(errors="replace")
    return any(mark in text for mark in REPLACEABLE)


def back_up_legacy(legacy: Path) -> None:
    """Move a `.legacy` hook aside, since `prek install -f` would delete it."""
    backup = free_name(legacy.with_name(f"{legacy.name}.tbhb-backup.{stamp()}"))
    legacy.rename(backup)
    print(
        f"{PROG}: moved {legacy} to {backup}; prek install -f would delete it",
        file=sys.stderr,
    )


def install_identity_hook(
    prek: list[str], top: Path, hooks: Path, pair: tuple[str, str]
) -> None:
    """Write one identity shim, chaining prek's own hook for the stage, if any."""
    name, verb = pair
    hook = hooks / name
    if not replaceable(hook):
        print(f"{PROG}: warning: {hook} is not ours; leaving it", file=sys.stderr)
        return
    hook.unlink(missing_ok=True)
    chained = hooks / f"{name}.prek"
    chained.unlink(missing_ok=True)
    # prek moves a hook it doesn't own to <hook>.legacy, so an overlapping
    # run could leave an identity shim there. Any other .legacy may be
    # someone's own hook, so it's kept, or backed up below.
    legacy = hooks / f"{name}.legacy"
    if legacy.is_file() and any(
        mark in legacy.read_text(errors="replace") for mark in SHIM_MARKS
    ):
        legacy.unlink()
    staged = prek_run(prek, top, "list", "--hook-stage", name)
    if staged.returncode == 0 and staged.stdout.strip():
        if legacy.is_file():
            back_up_legacy(legacy)
        prek_install(prek, top, "-f", "--hook-type", name)
        try:
            hook.rename(chained)
        except FileNotFoundError as exc:
            message = f"prek install -f --hook-type {name} wrote no {hook}"
            raise InstallError(message) from exc
    hook.write_text(
        SHIM.format(mark=SHIM_MARK, verb=verb, hook=name, origin=ORIGIN_GREP)
    )
    hook.chmod(0o755)


def require_pre_push_hook(hooks: Path) -> None:
    """Refuse a foreign pre-push hook before prek could move it."""
    hook = hooks / "pre-push"
    if not replaceable(hook):
        message = f"{hook} is not ours; move it aside and run the install again"
        raise InstallError(message)


def install_pre_push_hook(hooks: Path) -> None:
    """Chain the guard before prek, reading stdin once and replaying its bytes."""
    hook = hooks / "pre-push"
    require_pre_push_hook(hooks)
    hook.rename(hooks / "pre-push.prek")
    legacy = hooks / "pre-push.legacy"
    if legacy.is_file() and SHIM_MARK in legacy.read_text():
        legacy.unlink()
    hook.write_text(
        PRE_PUSH_SHIM.format(
            python=sys.executable,
            mark=SHIM_MARK,
            pattern=TBHB_DEV_ORIGIN.pattern,
        )
    )
    hook.chmod(0o755)


def install_git_hooks(
    cwd: Path | None, env: Mapping[str, str], *, dry_run: bool
) -> int:
    """Install the git hooks of the clone containing `cwd`."""
    code, out = git(
        "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir",
        cwd=cwd,
    )  # fmt: skip
    if code != 0:
        print(f"{PROG}: not in a git work tree; skipping the git hooks")
        return 0
    top, common = (Path(line) for line in out.split("\n"))
    if not in_tbhb_dev(top):
        print(f"{PROG}: origin is not a tbhb-dev repository; skipping the git hooks")
        return 0
    _, hooks_path = git(*HOOKS_PATH, cwd=top)
    if hooks_path:
        print(
            f"{PROG}: warning: core.hooksPath is {hooks_path};"
            " not installing git hooks",
            file=sys.stderr,
        )
        return 0
    hooks = common / "hooks"
    try:
        require_pre_push_hook(hooks)
        prek = find_prek()
        timeout = lock_timeout(env)
        if dry_run:
            names = ", ".join(name for name, _ in IDENTITY_HOOKS)
            print(
                "would install prek's pre-commit and pre-push hooks"
                f" and the {names} identity hooks in {hooks}"
            )
            return 0
        hooks.mkdir(exist_ok=True)
        with hooks_lock(hooks, timeout):
            require_pre_push_hook(hooks)
            prek_install(
                prek, top, "--hook-type", "pre-commit", "--hook-type", "pre-push"
            )
            install_pre_push_hook(hooks)
            for pair in IDENTITY_HOOKS:
                install_identity_hook(prek, top, hooks, pair)
    except InstallError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 1
    print(f"{PROG}: installed git hooks in {hooks}")
    return 0


# ---------------------------------------------------------------- command line


def build_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Install this clone's git hooks and merge the tbhbdev alpha hooks "
            "into each harness's user-level config."
        ),
    )
    parser.add_argument(
        "--harness", choices=HARNESSES, help="merge only this harness's config"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the changes, write nothing"
    )
    parser.add_argument(
        "--hooks-only",
        action="store_true",
        help="install only this clone's git hooks and write nothing at user level",
    )
    parser.add_argument(
        "--hook",
        action="store_true",
        help=(
            "SessionStart hook: put the gh shim on PATH and install the git "
            "hooks of the payload's worktree"
        ),
    )
    return parser


def session_start(env: Mapping[str, str], *, dry_run: bool) -> int:
    """`--hook`: the shim on PATH and the payload worktree's git hooks.

    It prints on stderr only: Claude Code adds a SessionStart hook's stdout
    to the model's context. A failure is one line on stderr, and the status
    is always 0, so a broken install never blocks a session.
    """
    try:
        with redirect_stdout(sys.stderr):
            export_shim_path(env, dry_run=dry_run)
            cwd = hook_cwd(read_hook_stdin())
            if in_tbhb_dev(cwd):
                install_git_hooks(cwd, env, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 -- fail open on any crash
        print(f"{PROG}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


def main(argv: list[str], prog: str) -> int:
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    env = os.environ
    if args.hooks_only:
        if args.hook:
            parser.error("--hooks-only cannot be used with --hook")
        if args.harness:
            parser.error("--hooks-only cannot be used with --harness")
        return install_git_hooks(None, env, dry_run=args.dry_run)
    if args.hook:
        return session_start(env, dry_run=args.dry_run)
    status = install_git_hooks(None, env, dry_run=args.dry_run)
    status |= install_gh_shim(dry_run=args.dry_run)
    for harness in [args.harness] if args.harness else HARNESSES:
        result = install_config(harness, env, dry_run=args.dry_run)
        status |= result
        if harness == CODEX:
            status |= install_codex_environment(
                env, dry_run=args.dry_run, in_trust_diff=result == 0
            )
    return status | install_private_files(dry_run=args.dry_run)
