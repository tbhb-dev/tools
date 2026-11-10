"""Text for analytics command results and safe failure diagnostics."""

from typing import TYPE_CHECKING

from tbhbdev.analytics.decisions.diagnostic import diagnostics

if TYPE_CHECKING:
    from tbhbdev.analytics.model import Migration, Verdict


def migrated(pending: tuple[Migration, ...], *, dry_run: bool) -> str:
    action = "pending" if dry_run else "applied"
    return f"analytics: {len(pending)} {action} migrations\n" + "".join(
        f"{migration.version}: {migration.name}\n" for migration in pending
    )


def checked(verdict: Verdict) -> str:
    if verdict.violations:
        return "".join(f"analytics: {line}\n" for line in verdict.violations)
    return "analytics: catalog matches contract\n"


def failed(error: Exception) -> str:
    return "\n".join((*diagnostics(str(error)), "analytics: command failed", ""))
