"""Check agent attribution and signing headers across a local commit range."""

import argparse
import subprocess  # nosec B404
import sys

from tbhbdev.alpha import identity
from tbhbdev.alpha.agent.transport import terminal_text


def problems(
    author: str, committer: str, parents: int, *, signed: bool, message: str
) -> list[str]:
    """Return every failed rule using only parsed commit values."""
    reasons = []
    email = identity.ident_email(identity.AGENT_IDENT)
    for role, address in (("author", author), ("committer", committer)):
        if address != email:
            reasons.append(f"{role} email is {address!r}; expected {email}")
        if address.lower().endswith("@users.noreply.github.com") or address.lower() == (
            "noreply@github.com"
        ):
            reasons.append(f"{role} uses a GitHub noreply address")
    if not signed:
        reasons.append("missing gpgsig header")
    if identity.SIGNOFF.search(message):
        reasons.append(identity.SIGNOFF_ERROR)
    reasons.extend(identity.check_message(message))
    if identity.pick_refused(identity.llm_trailers(message), own=True):
        reasons.append(f"missing {identity.KEY_GEN} trailer with model=")
    if parents != 1:
        reasons.append(f"expected one parent, found {parents}")
    return reasons


def inspect_commit(text: str) -> tuple[list[str], str]:
    """Parse a commit object, keeping continuation lines out of its headers."""
    headers, _, message = text.partition("\n\n")
    # A continuation starts with a space, so its key is empty and cannot add
    # an author, committer, signature or parent header.
    fields = [line.partition(" ") for line in headers.split("\n")]
    values = {}
    for key, _, value in fields:
        values.setdefault(key, value)
    reasons = problems(
        identity.ident_email(values.get("author", "")),
        identity.ident_email(values.get("committer", "")),
        sum(key == "parent" for key, _, _ in fields),
        signed="gpgsig" in values or "gpgsig-sha256" in values,
        message=message,
    )
    subject = " ".join(message.lstrip("\n").partition("\n\n")[0].splitlines())
    return reasons, subject


def git_output(path: str, *args: str) -> str:
    """Read original Git objects as UTF-8 without newline translation."""
    return subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-C",
            path,
            *args,
        ],
        check=True,
        capture_output=True,
        text=False,
    ).stdout.decode(errors="replace")


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("base", metavar="BASE")
    parser.add_argument("tip", metavar="TIP")
    parser.add_argument("-C", dest="path", default=".")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        base, tip = (
            git_output(
                args.path,
                "rev-parse",
                "--verify",
                "--end-of-options",
                ref + "^{commit}",
            ).strip()
            for ref in (args.base, args.tip)
        )
        commits = git_output(
            args.path, "rev-list", "--reverse", "--topo-order", f"{base}..{tip}", "--"
        ).splitlines()
        failed = 0
        for sha in commits:
            reasons, subject = inspect_commit(
                git_output(args.path, "cat-file", "commit", sha)
            )
            if reasons:
                failed += 1
                result = (
                    "FAIL " + "; ".join(terminal_text(why) for why in reasons) + " |"
                )
            else:
                result = "ok"
            print(f"{sha[:7]} {result} {terminal_text(subject)}")
        print(f"verify-range: {len(commits)} commits, {failed} failed")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"{prog}: {terminal_text(str(exc))}", file=sys.stderr)
        return 2
    return int(failed != 0)
