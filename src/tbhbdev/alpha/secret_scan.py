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
without the canary rule, or a missing .gitleaks.toml stops the command, and so
does any finding or gitleaks error. A finding is reported by its rule ID and
line only: gitleaks redacts its JSON report, and nothing else it prints about a
finding is passed on.
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

from tbhbdev.host import CANARY, GITLEAKS_CANARY, GITLEAKS_EXTENDS, GITLEAKS_FILE

# The remedy for a missing private config: the host's provisioning installs it.
FETCH = "Install it with the host's provisioning tooling."


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


# The private config's pointer to the committed config, and a committed
# config's own [extend] table.
EXTENDS_PATH = re.compile(r"^(\s*path\s*=\s*)(?:\"[^\"]*\"|'[^']*')", re.MULTILINE)
EXTEND_TABLE = re.compile(r"^\s*\[extend\]", re.MULTILINE)


def with_base(committed: str) -> str:
    """The committed config with gitleaks' default rules as its base.

    A repository that has dropped ``[extend] useDefault`` gets its base from
    the Bazel wrapper, which a text scan can't run. Without a base, an
    allowlist-only ``[[rules]]`` override fails to load. A config that has its
    own ``[extend]`` table keeps it.
    """
    if EXTEND_TABLE.search(committed):
        return committed
    return committed + "\n[extend]\nuseDefault = true\n"


def private_config() -> Path:
    """The host's private gitleaks config."""
    return Path.home() / GITLEAKS_FILE


def findings(report: str) -> list[tuple[int, str]] | None:
    """Each finding's line and rule ID from gitleaks' JSON report, or None.

    They are sorted, since gitleaks reports them in no fixed order.

    None means the report is not a JSON list, so gitleaks did not scan.
    """
    try:
        data = json.loads(report)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    entries = cast("list[dict[str, int | str]]", data)  # pragma: no mutate
    return sorted((int(entry["StartLine"]), str(entry["RuleID"])) for entry in entries)


def is_name_rule(rule: dict[str, object]) -> bool:
    """Whether a rule belongs to one of the ``NAME_RULES`` families."""
    return re.sub(r"-\d+$", "", str(rule.get("id"))) in NAME_RULES


def without_name_rules(private: str) -> str | None:
    """The private config minus its name rules, or None if it doesn't parse.

    None sends the scan to the full config, where gitleaks reports the
    parse failure itself.
    """
    try:
        data = tomllib.loads(private)
    except tomllib.TOMLDecodeError:
        return None
    rules = data.get("rules", [])
    data["rules"] = [rule for rule in rules if not is_name_rule(rule)]
    return tomlkit.dumps(data)


def run_gitleaks(
    text: str, what: str, config: Path, *, root: Path, gitleaks: str
) -> None:
    """Refuse text that gitleaks flags under a config, or when it fails."""
    argv = [
        gitleaks, "stdin", "--redact", "--no-banner", "--config", str(config),
        "--report-format", "json", "--report-path", "-",
    ]  # fmt: skip
    # Argv is the found gitleaks and fixed flags; text goes on stdin. NO_COLOR
    # keeps escape codes out of the log lines a failure passes on.
    done = subprocess.run(  # nosec B603
        argv,
        capture_output=True,
        text=True,
        check=False,  # pragma: no mutate -- the default, spelled for the linter
        cwd=root,
        input=text,
        env=os.environ | {"NO_COLOR": "1"},
    )
    found = findings(done.stdout)
    if found:
        where = "; ".join(f"rule {rule} on line {line}" for line, rule in found)
        message = f"refusing {what}: gitleaks finds a secret: {where}"
        raise SystemExit(message)
    if done.returncode != 0 or found is None:
        output = " ".join(done.stderr.split())
        message = (
            f"cannot scan {what} for secrets: gitleaks failed "
            f"(exit {done.returncode}): {output}"
        )
        raise SystemExit(message)


def scan(text: str, what: str, *, root: Path, gitleaks: str, visibility: str) -> None:
    """Refuse text that gitleaks flags under the private config.

    Args:
        text: The text to scan.
        what: What the text is, for the refusal message.
        root: The repository root, which holds .gitleaks.toml.
        gitleaks: The gitleaks executable.
        visibility: The visibility, as GitHub reports it, of the repository the
            text is posted to, not the working directory's. Only
            ``PRIVATE`` skips the name rules; anything else, including an
            empty value, scans under the full private config.

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
    if not CANARY.search(private):
        message = (
            f"cannot scan {what} for secrets: {config} has no rule with the id "
            f"{GITLEAKS_CANARY}, so it is not the private gitleaks config. {FETCH}"
        )
        raise SystemExit(message)
    if not (root / GITLEAKS_EXTENDS).is_file():
        message = (
            f"cannot scan {what} for secrets: {root / GITLEAKS_EXTENDS} does not exist"
        )
        raise SystemExit(message)
    reduced = without_name_rules(private) if visibility == PRIVATE else None
    with tempfile.TemporaryDirectory() as directory:  # mode 0700
        base = Path(directory) / "committed.toml"
        copy = Path(directory) / "private.toml"
        for path in (base, copy):
            path.touch()
            path.chmod(0o600)  # touch's mode is cut by the umask, chmod's is not
        base.write_text(with_base((root / GITLEAKS_EXTENDS).read_text()))
        # The copies point at the committed copy by absolute path: the config
        # path stays the root's, and gitleaks runs from there.
        copy.write_text(
            EXTENDS_PATH.sub(lambda m: f"{m.group(1)}'{base}'", reduced or private)
        )
        run_gitleaks(text, what, copy, root=root, gitleaks=gitleaks)
