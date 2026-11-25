"""Parse and update the installer configurations using their in-memory values."""

import json
import shlex
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

type Json = dict[str, object]

MARK = "# tbhbdev alpha install"
# The environment both harnesses give their commands: no SSH agent, so a
# command can't reach the 1Password SSH agent and prompt the operator.
AGENT_ENVIRONMENT = {"SSH_AUTH_SOCK": ""}
POLICY = "shell_environment_policy"

COLUMNS_PREFIX = (
    "COLUMNS=${COLUMNS:-$(tmux display-message -p '#{pane_width}' 2>/dev/null"
    " || echo 80)}"
)
BUNX_COMMAND = "bunx -y ccstatusline@latest"
# The two forms of the registry-resolving command this installer replaces.
OLD_STATUSLINES = (
    f"PATH=/opt/homebrew/bin:$PATH {COLUMNS_PREFIX} {BUNX_COMMAND}",
    BUNX_COMMAND,
)
STATUSLINE_TOKENS = 2  # the node path and the script


class InstallError(Exception):
    """A failure that stops the install, with the message to print."""


def parse_config(text: str | None) -> Json:
    """Parse JSON config text, using an empty object for missing or blank text."""
    if text is None or not text.strip():
        return {}
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        message = f"it is not valid JSON ({exc})"
        raise InstallError(message) from exc
    if not isinstance(loaded, dict):
        message = "it is not a JSON object"
        raise InstallError(message)
    return cast("Json", loaded)  # pragma: no mutate


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


def with_environment(old: str | None) -> str:
    """Codex's config.toml text OLD (None when missing) with AGENT_ENVIRONMENT.

    The environment goes in shell_environment_policy.set. tomlkit keeps every
    other key, comment and the file's layout. Import it only when editing the config.
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


def trusted_config(old: str | None, hooks: Path, wanted: Mapping[str, str]) -> str:
    """Update Codex trust and, when possible, merge the command environment."""
    from tbhbdev.alpha import codex_trust  # noqa: PLC0415 -- loads tomlkit

    new = codex_trust.trusted_text(old, hooks, wanted)
    with suppress(InstallError):
        new = with_environment(new)
    return new


def statusline_script(root: Path) -> Path:
    """The package's bin, `dist/ccstatusline.js` in its package.json."""
    return root / "node_modules" / "ccstatusline" / "dist" / "ccstatusline.js"


def statusline_command(node: str, script: Path) -> str:
    return f"{COLUMNS_PREFIX} {shlex.quote(node)} {shlex.quote(str(script))}"


def statusline_package(root: Path) -> Path:
    """The installed package's directory."""
    return root / "node_modules" / "ccstatusline"


def missing_tool(node: str | None, bun: str | None, *, installed: bool) -> str | None:
    """Name the tool the pin can't run without, or None when it has both."""
    if not node:
        return "node"
    return "bun" if not installed and not bun else None


def is_ours(command: str, base: Path) -> bool:
    """Whether COMMAND is exactly what this installer writes, for any node and version.

    The node slot must be an absolute path named `node`, the script the pinned
    package's bin under a version directory in BASE, and the
    quoting the installer's own. So `$MY_STATUS_NODE` and `evil-binary` don't match,
    and a command written for an older version does.
    """
    head = f"{COLUMNS_PREFIX} "
    if not command.startswith(head):
        return False
    try:
        tokens = shlex.split(command[len(head) :])
    except ValueError:
        return False
    if len(tokens) != STATUSLINE_TOKENS:
        return False
    node, script = Path(tokens[0]), Path(tokens[1])
    return (
        node.is_absolute()
        and node.name == "node"
        and script.is_absolute()
        # Any root gives the same last four parts, so mutating "/" changes nothing.
        and script.parts[-4:]
        == statusline_script(Path("/")).parts[-4:]  # pragma: no mutate
        and script.parents[3].parent == base
        and command == statusline_command(tokens[0], script)
    )


def replaceable_line(config: Json, base: Path) -> Json | None:
    """The `statusLine` object when its command is a known or our own one."""
    line = config.get("statusLine")
    if not isinstance(line, dict):
        return None
    command = line.get("command")
    if not isinstance(command, str):
        return None
    known = command in OLD_STATUSLINES or is_ours(command, base)
    return line if known else None


def pinned_statusline(config: Json, base: Path, wanted: str | None) -> Json | None:
    """Replace only the command of a status line this installer can pin."""
    line = replaceable_line(config, base)
    if line is None:
        return None
    return {**config, "statusLine": {**line, "command": wanted}}
