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
    """The text was not cleared: a finding, a scanner failure or a missing config.

    ``kind`` is ``finding``, ``config``, ``scanner_missing``, ``signal`` or
    ``exit``. ``lines`` holds a finding's line numbers, and ``status`` the exit
    status of a failed scanner. Callers choose their wording from these fields
    and need not parse the message.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "exit",
        locations: str | None = None,
        lines: tuple[int, ...] = (),
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.locations = locations
        self.lines = lines
        self.status = status


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
            It is raised after the handler ends, so it carries no chained
            exception and no scanner text beyond its own fields.
    """
    refusal: ScanRefused | None = None
    try:
        secret_scan.scan(
            text, what, root=root, gitleaks=gitleaks, visibility=visibility
        )
    except secret_scan.FindingRefusal as exc:
        refusal = ScanRefused(
            str(exc.code), kind="finding", locations=exc.locations, lines=exc.lines
        )
    except secret_scan.ScanBlocked as exc:
        refusal = ScanRefused(str(exc.code), kind=exc.kind, status=exc.status)
    except SystemExit as exc:
        refusal = ScanRefused(str(exc.code))
    except OSError as exc:
        message = (
            f"cannot scan {what} for secrets: the scanner {gitleaks} "
            f"cannot run ({exc.strerror})"
        )
        refusal = ScanRefused(message, kind="scanner_missing")
    if refusal is not None:
        raise refusal
