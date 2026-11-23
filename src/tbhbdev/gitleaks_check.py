# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Public gitleaks scan of text before a tool posts it.

``scan_text`` is the stable entry point for tools that post agent-written text.
It runs the same fail-closed scan as ``tbhbdev alpha pr`` and ``tbhbdev alpha
merge``, so one path decides what text may be posted. The scan, its private
config and its visibility rules are documented in ``tbhbdev.alpha.secret_scan``.
"""

from typing import TYPE_CHECKING

from tbhbdev.alpha import secret_scan

if TYPE_CHECKING:
    from pathlib import Path


class ScanRefused(RuntimeError):  # noqa: N818 - the public name callers import
    """The text was not cleared: a finding, a scanner failure or a missing config."""

    def __init__(self, message: str, *, locations: str | None = None) -> None:
        super().__init__(message)
        self.locations = locations


def scan_text(
    text: str, what: str, *, root: Path, gitleaks: str, visibility: str
) -> None:
    """Refuse text that gitleaks flags under the private config.

    Args:
        text: The text to scan.
        what: What the text is, for the refusal message.
        root: The repository root, which holds .gitleaks.toml.
        gitleaks: The gitleaks executable.
        visibility: The visibility, as GitHub reports it, of the repository the
            text is posted to. Only ``PRIVATE`` skips the name rules.

    Raises:
        ScanRefused: The scan found a secret or couldn't run, a missing or
            non-executable scanner included. The message names rule IDs and line
            numbers, or the executable and the OS reason, never matched text.
    """
    try:
        secret_scan.scan(
            text, what, root=root, gitleaks=gitleaks, visibility=visibility
        )
    except secret_scan.FindingRefusal as exc:
        raise ScanRefused(str(exc.code), locations=exc.locations) from None
    except SystemExit as exc:
        raise ScanRefused(str(exc.code)) from None
    except OSError as exc:
        message = (
            f"cannot scan {what} for secrets: the scanner {gitleaks} "
            f"cannot run ({exc.strerror})"
        )
        raise ScanRefused(message) from None
