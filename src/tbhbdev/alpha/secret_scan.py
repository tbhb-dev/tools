# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Secret scan of agent-written text, ported from an earlier interim toolset.

``tbhbdev alpha pr`` scans a pull request's final title and body, and
``tbhbdev alpha merge`` its squash message, before gh posts them. The scan
pipes the text through ``gitleaks stdin`` under the host's private config,
``private.toml`` in ``.config/gitleaks`` under the home directory, which
extends the repository's .gitleaks.toml. That is the combination
tbhb_rules' ``gitleaks_test`` uses for its private config: the private file
begins with ``[extend] path = ".gitleaks.toml"``, gitleaks runs from the
repository root so the path resolves, and the private file must define the
``tbhb-private-canary`` rule. The host's provisioning tooling installs that file.

Text posted to a private repository skips the private config's name rules, the
rule families in ``NAME_RULES``. The visibility is that of the repository the
text is posted to, not the working directory's; the caller reads it. The rules'
gates apply the private config only to a repository that sets
``--@tbhb_rules//lint:gitleaks_private_config``, so text that names a private
repository's own subject, such as a host it documents or a sibling private
repository, passes there. The scan then runs gitleaks under a temporary copy of
the private config without those rules, in a 0700 directory as a 0600 file,
removed afterwards. The copy keeps the ``[extend]`` path, so the repository's
.gitleaks.toml and gitleaks' default rules still apply. So do every other
private rule, such as the ``op-*`` rules, the tailnet and private addresses, the
MAC address, the endpoint and keychain rules and the canary. Only the exact
visibility ``PRIVATE`` skips them. Any other value, ``PUBLIC`` and ``INTERNAL``
included, an empty one or a failed read, gets the full private config.

There is no public-only fallback. A missing or unreadable private config, one
that doesn't parse or lacks the canary rule and one other rule, or a missing
.gitleaks.toml stops the command, and so does any finding or gitleaks error. A
finding is reported by its rule ID and line only, with the file for a scan of
staged changes: gitleaks redacts its JSON report. Nothing else gitleaks prints
is passed on, not even on a failure, where the message gives only its exit
status: its error output can quote a private rule's regex.

``tbhbdev alpha pre-commit-scan`` scans a commit's staged changes the same way,
with ``STAGED`` in place of ``STDIN`` (see ``tbhbdev.alpha.pre_commit_scan``).
That scan reads .gitleaks.toml and .gitleaksignore from the index, not the
work tree, so an unstaged edit to either can't loosen the scan of what the
commit records. A staged change to either file does apply to the commit that
stages it. gitleaks runs in, and scans, a 0700 directory that holds the
index copies beside the private config, or its copy without the name rules,
and ``GIT_DIR`` and ``GIT_WORK_TREE`` point its git at the repository.
Without .gitleaks.toml in the index, the private config's ``[extend]`` path
gives way to gitleaks' default rules, so no work tree file is read.
"""

import json
import os
import re
import subprocess  # nosec B404
import tempfile
import tomllib
from pathlib import Path
from typing import cast

import tomlkit

from tbhbdev.alpha.repo import git
from tbhbdev.host import GITLEAKS_CANARY, GITLEAKS_EXTENDS, GITLEAKS_FILE

# The remedy for a missing private config: the host's provisioning installs it.
FETCH = "Install it with the host's provisioning tooling."


# What gitleaks scans: the text on stdin, or the staged changes of the work
# tree named after these flags, which `git diff --staged` reads from the index.
STDIN = ("stdin",)
STAGED = ("git", "--pre-commit", "--staged")

# The fingerprints gitleaks ignores, read from the path it scans.
GITLEAKS_IGNORE = ".gitleaksignore"

# The only visibility that skips the name rules. Anything else, even a
# lower-case "private", gets the full private config.
PRIVATE = "PRIVATE"

# The private config's name rules: each rule id without its trailing
# ``-<digits>`` is one of these. They name a private repository's own
# subject (its sibling repositories, hosts, tailnet, local names, home and
# tool directories). The rules' gates apply the private config only to a
# repository that sets the flag, so a private repository's text skips them.
# Every other rule stays: the ``op-*`` rules, ``tailnet-ipv4``,
# ``tailnet-ipv6``, ``private-ipv4``, ``mac-address``, ``endpoint``,
# ``keychain``, the canary and whatever .gitleaks.toml defines.
NAME_RULES = (
    "private-repo",
    "tailnet-host",
    "tailnet-stem",
    "tailnet-name",
    "tailnet-suffix",
    "local-name",
    "home-path",
    "tool-dir",
)


def private_config() -> Path:
    """The host's private gitleaks config."""
    return Path.home() / GITLEAKS_FILE


def findings(report: str) -> list[tuple[str, int, str]] | None:
    """Each finding's file, line and rule ID from gitleaks' JSON report, or None.

    They are sorted, since gitleaks reports them in no fixed order. The file is
    empty for text scanned on stdin.

    None means the report is not a JSON list of findings that each have an
    integer ``StartLine`` and a string ``RuleID``, so gitleaks did not scan.
    """
    try:
        data = json.loads(report)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    entries = cast("list[object]", data)  # pragma: no mutate
    found: list[tuple[str, int, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        fields = cast("dict[str, object]", entry)  # pragma: no mutate
        line = fields.get("StartLine")
        rule = fields.get("RuleID")
        if not isinstance(line, int) or not isinstance(rule, str):
            return None
        found.append((str(fields.get("File", "")), line, rule))
    return sorted(found)


def place(path: str, line: int) -> str:
    """Where a finding is: ``on line N`` for stdin, ``in PATH:N`` for a file.

    A path shows with control characters and non-ASCII escaped, so a filename
    can't forge output lines.
    """
    # Codec names match without regard to case, so upper-casing one is equivalent.
    shown = path.encode("unicode_escape").decode("ascii")  # pragma: no mutate
    return f"in {shown}:{line}" if path else f"on line {line}"


def is_name_rule(rule: dict[str, object]) -> bool:
    """Whether a rule belongs to one of the ``NAME_RULES`` families."""
    return re.sub(r"-\d+$", "", str(rule.get("id"))) in NAME_RULES


def without_name_rules(data: dict[str, object]) -> str:
    """The parsed private config minus its name rules, as TOML."""
    rules = cast("list[dict[str, object]]", data["rules"])  # pragma: no mutate
    return tomlkit.dumps(
        {**data, "rules": [rule for rule in rules if not is_name_rule(rule)]}
    )


def parse_private(private: str) -> dict[str, object] | None:
    """The private config, or None unless it holds the canary and another rule.

    The canary must be a rule's ``id``, not just text somewhere in the file,
    and a config of the canary alone has no rule to scan with.
    """
    try:
        data = tomllib.loads(private)
    except tomllib.TOMLDecodeError:
        return None
    rules = data.get("rules")
    if not isinstance(rules, list):
        return None
    listed = cast("list[object]", rules)  # pragma: no mutate
    ids = [
        cast("dict[str, object]", rule).get("id")  # pragma: no mutate
        for rule in listed
        if isinstance(rule, dict)
    ]
    other = any(isinstance(id_, str) and id_ != GITLEAKS_CANARY for id_ in ids)
    if GITLEAKS_CANARY not in ids or not other:
        return None
    return data


def staged_file(root: Path, name: str, what: str) -> str | None:
    """The file NAME in the index at ROOT, or None when the index has none.

    A path that is unmerged or a symlink is refused: the first has no stage 0
    to read, so reading none would drop the repository's rules, and the second
    holds only its target's name, not the target's rules.
    """
    message = f"cannot scan {what} for secrets: git cannot read {name} from the index"
    code, listed = git("ls-files", "--stage", "--", f":(literal){name}", cwd=root)
    if code != 0:
        raise SystemExit(message)
    if not listed:
        return None
    entries = [line.split(None, 3) for line in listed.splitlines()]
    if len(entries) != 1 or entries[0][0] == "120000" or entries[0][2] != "0":
        raise SystemExit(message)
    code, text = git("cat-file", "blob", entries[0][1], cwd=root)
    if code != 0:
        raise SystemExit(message)
    return text


# What gitleaks' own ``git diff --staged`` must not do: colour its output
# (``always`` makes gitleaks read no diff and report nothing) or pair a
# removal with an addition (a rename moves content out of an allowlisted path
# unscanned).
DIFF_CONFIG = (
    ("color.ui", "never"),
    ("color.diff", "never"),
    ("diff.renames", "false"),
)


def diff_env(root: Path, what: str, attributes: Path) -> dict[str, str]:
    """The environment that makes gitleaks' ``git diff`` show the additions.

    ATTRIBUTES is a file that marks every path as text, so that a NUL byte or
    a ``binary`` attribute does not reduce an addition to "Binary files
    differ". The committed ``.gitattributes`` is ignored by reading attributes
    from the empty tree. A repository's ``.git/info/attributes`` still applies,
    because ``GIT_ATTR_SOURCE`` doesn't cover it, so this is hardening against
    committed files only, and a local write to ``.git`` can defeat it.
    """
    try:
        start = int(os.environ.get("GIT_CONFIG_COUNT") or "0")
    except ValueError:
        start = -1
    if start < 0:
        message = f"cannot scan {what} for secrets: GIT_CONFIG_COUNT is not a count"
        raise SystemExit(message)
    code, empty = git("hash-object", "-t", "tree", "/dev/null", cwd=root)
    if code != 0 or not empty:
        message = f"cannot scan {what} for secrets: git cannot name the empty tree"
        raise SystemExit(message)
    private_file(attributes, "* diff\n")
    settings = (*DIFF_CONFIG, ("core.attributesFile", str(attributes)))
    # GIT_CONFIG_PARAMETERS (what `git -c` sets) outranks GIT_CONFIG_COUNT, so a
    # `git -c color.diff=always commit` would colour the diff and hide the
    # additions from gitleaks. Emptying it drops those settings from this scan.
    env = {
        "GIT_ATTR_SOURCE": empty,
        "GIT_CONFIG_COUNT": str(start + len(settings)),
        "GIT_CONFIG_PARAMETERS": "",
    }
    for offset, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{start + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    return env


def staged_env(root: Path, what: str) -> dict[str, str]:
    """The environment that points gitleaks' git at the repository at ROOT.

    gitleaks reads ``.gitleaksignore`` from the path it scans, so it scans the
    private directory, and ``GIT_DIR`` and ``GIT_WORK_TREE`` lead its git to
    the repository. A relative ``GIT_INDEX_FILE`` from the hook becomes
    absolute against ROOT, where the hook's git ran and where staged_file reads.
    """
    code, git_dir = git("rev-parse", "--absolute-git-dir", cwd=root)
    if code != 0:
        message = f"cannot scan {what} for secrets: git cannot find the repository"
        raise SystemExit(message)
    env = os.environ | {"GIT_DIR": git_dir, "GIT_WORK_TREE": str(root)}
    index = os.environ.get("GIT_INDEX_FILE")
    if index:
        env["GIT_INDEX_FILE"] = os.path.abspath(root / index)  # noqa: PTH100 -- a str
    return env


def run_gitleaks(  # noqa: PLR0913 -- the scan inputs, keyword-only past two
    text: str,
    what: str,
    config: Path,
    *,
    root: Path,
    gitleaks: str,
    source: tuple[str, ...] = STDIN,
    tree: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Refuse what gitleaks flags under a config, or when it fails.

    gitleaks runs in ROOT with ENV, or this process's environment. A
    ``STAGED`` scan also names TREE, the path it scans.
    """
    argv = [
        gitleaks, *source, "--redact", "--no-banner", "--config", str(config),
        "--report-format", "json", "--report-path", "-",
        *([] if tree is None else [str(tree)]),
    ]  # fmt: skip
    # Argv is the found gitleaks, fixed flags and the path it scans; text goes
    # on stdin. Its output is captured and never shown.
    done = subprocess.run(  # nosec B603
        argv,
        capture_output=True,
        text=True,
        check=False,  # pragma: no mutate -- the default, spelled for the linter
        cwd=root,
        input=text,
        env=env,
    )
    found = findings(done.stdout)
    if found:
        where = "; ".join(
            f"rule {rule} {place(path, line)}" for path, line, rule in found
        )
        message = f"refusing {what}: gitleaks finds a secret: {where}"
        raise SystemExit(message)
    if done.returncode != 0 or found is None:
        message = (
            f"cannot scan {what} for secrets: gitleaks failed (exit {done.returncode})"
        )
        raise SystemExit(message)


def scan(  # noqa: PLR0913 -- the scan inputs, keyword-only past two
    text: str,
    what: str,
    *,
    root: Path,
    gitleaks: str,
    visibility: str,
    source: tuple[str, ...] = STDIN,
) -> None:
    """Refuse text that gitleaks flags under the private config.

    Args:
        text: The text to scan, sent on stdin; ignored for ``STAGED``.
        what: What the text is, for the refusal message.
        root: The repository root, which holds .gitleaks.toml.
        gitleaks: The gitleaks executable.
        visibility: The visibility, as GitHub reports it, of the repository the
            text is posted to, not the working directory's. Only
            ``PRIVATE`` skips the name rules; anything else, including an
            empty value, scans under the full private config.
        source: ``STDIN`` for the text, or ``STAGED`` for the staged changes of
            the work tree at ``root``.

    Raises:
        SystemExit: The private config or .gitleaks.toml is missing or wrong,
            or gitleaks reports a finding or fails.
    """
    config = private_config()
    try:
        private = config.read_text()
    except OSError as exc:
        message = (
            f"cannot scan {what} for secrets: the private gitleaks config "
            f"{config} cannot be read ({exc.strerror}). {FETCH}"
        )
        raise SystemExit(message) from None
    data = parse_private(private)
    if data is None:
        message = (
            f"cannot scan {what} for secrets: {config} does not parse to rules "
            f"with the id {GITLEAKS_CANARY} and at least one other, so it is not "
            f"the private gitleaks config. {FETCH}"
        )
        raise SystemExit(message)
    if source == STAGED:
        scan_staged(
            data, private, what, root=root, gitleaks=gitleaks, visibility=visibility
        )
        return
    if not (root / GITLEAKS_EXTENDS).is_file():
        message = (
            f"cannot scan {what} for secrets: {root / GITLEAKS_EXTENDS} does not exist"
        )
        raise SystemExit(message)
    if visibility != PRIVATE:
        run_gitleaks(text, what, config, root=root, gitleaks=gitleaks)
        return
    with tempfile.TemporaryDirectory() as directory:  # mode 0700
        copy = private_file(Path(directory) / "private.toml", without_name_rules(data))
        run_gitleaks(text, what, copy, root=root, gitleaks=gitleaks)


def private_file(path: Path, text: str) -> Path:
    """Write TEXT to a new 0600 file at PATH and return PATH."""
    path.touch()
    path.chmod(0o600)  # touch's mode is cut by the umask, chmod's is not
    path.write_text(text)
    return path


def scan_staged(  # noqa: PLR0913 -- the scan inputs, keyword-only past three
    data: dict[str, object],
    private: str,
    what: str,
    *,
    root: Path,
    gitleaks: str,
    visibility: str,
) -> None:
    """Refuse staged changes that gitleaks flags, with its files from the index.

    gitleaks resolves the ``[extend]`` path from its working directory and
    reads ``.gitleaksignore`` from the path it scans, so it runs in, and scans,
    a private directory that holds the index's copies of both beside the
    config. Without .gitleaks.toml in the index, the config extends gitleaks'
    default rules instead.
    """
    extends = staged_file(root, GITLEAKS_EXTENDS, what)
    ignore = staged_file(root, GITLEAKS_IGNORE, what)
    env = staged_env(root, what)
    reduced = visibility == PRIVATE
    with tempfile.TemporaryDirectory() as directory:  # mode 0700
        here = Path(directory)
        if ignore is not None:
            private_file(here / GITLEAKS_IGNORE, ignore)
        if extends is None:
            data = {**data, "extend": {"useDefault": True}}
            text = without_name_rules(data) if reduced else tomlkit.dumps(data)
        else:
            private_file(here / GITLEAKS_EXTENDS, extends)
            text = without_name_rules(data) if reduced else private
        copy = private_file(here / "private.toml", text)
        env = env | diff_env(root, what, here / "attributes")
        run_gitleaks(
            "",
            what,
            copy,
            root=here,
            gitleaks=gitleaks,
            source=STAGED,
            tree=here,
            env=env,
        )
