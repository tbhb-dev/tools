# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Secret scan of agent-written text, ported from ascm's interim tools.

``tbhbdev alpha pr`` scans a pull request's final title and body, and
``tbhbdev alpha merge`` its squash message, before gh posts them. The scan
pipes the text through ``gitleaks stdin`` under the host's private config,
``private.toml`` in ``.config/gitleaks`` under the home directory, which
extends the repository's .gitleaks.toml. That is the combination
tbhb_rules' ``gitleaks_test`` uses for its private config: the private file
begins with ``[extend] path = ".gitleaks.toml"``, gitleaks runs from the
repository root so the path resolves, and the private file must define the
``tbhb-private-canary`` rule. ``tbhbdev host upgrade`` installs that file.

There is no public-only fallback. A missing or unreadable private config, one
without the canary rule, or a missing .gitleaks.toml stops the command, and so
does any finding or gitleaks error. A finding is reported by its rule ID and
line only: gitleaks redacts its JSON report, and nothing else it prints about a
finding is passed on.
"""

import json
import os
import subprocess  # nosec B404
from pathlib import Path
from typing import cast

from tbhbdev.host import CANARY, GITLEAKS_CANARY, GITLEAKS_EXTENDS, GITLEAKS_FILE

# The host step that writes the private config. Agents run upgrade, which
# refreshes it on every run.
FETCH = "Run `tbhbdev host upgrade` (step gitleaks-config), which writes it."


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
    entries = cast("list[dict[str, object]]", data)  # pragma: no mutate
    return sorted(
        (cast("int", entry["StartLine"]), str(entry["RuleID"])) for entry in entries
    )


def scan(text: str, what: str, *, root: Path, gitleaks: str) -> None:
    """Refuse text that gitleaks flags under the private config.

    Args:
        text: The text to scan.
        what: What the text is, for the refusal message.
        root: The repository root, which holds .gitleaks.toml.
        gitleaks: The gitleaks executable.

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
