# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha pre-commit-scan`: scan the staged changes for secrets.

The pre-commit shim that `tbhbdev alpha install` writes runs this before
prek's own pre-commit hook. It runs ``gitleaks git --pre-commit --staged``
under the host's private gitleaks config, the scan ``tbhbdev alpha pr`` gives
pull request text (see ``tbhbdev.alpha.secret_scan``), from the top of the work
tree. gitleaks reads the staged diff with git, which inherits the hook's
``GIT_INDEX_FILE``, so ``git commit -a`` and ``git commit PATH`` scan the index
the commit records. Only added lines count.

A commit hook doesn't call gh, so the visibility comes from the same local
lists the pre-push guard uses (see ``tbhbdev.alpha.private_names``): origin
named by ``[pr_check] private_repos`` or the host's private repository list,
or with a ``-private`` component, counts as ``PRIVATE`` and skips the name
rules. Any other origin, a missing one, or a host list that can't be read
scans under the full private config.

It fails closed like the pull request scan: a missing or unreadable private
config, one without the canary rule, a missing .gitleaks.toml, no gitleaks on
PATH or through mise, a gitleaks failure or any finding refuses the commit. A
finding is reported by its rule ID, file and line only.
"""

import sys

from tbhbdev.alpha import private_names, pull_request, secret_scan


def visibility() -> str:
    """``PRIVATE`` when origin is a private repository by the local lists, else ""."""
    try:
        names = private_names.private_names()
    except private_names.NameListError:
        return ""
    return secret_scan.PRIVATE if names is None else ""


def main(argv: list[str], prog: str) -> int:
    if argv:
        print(f"usage: {prog}", file=sys.stderr)
        return 2
    try:
        secret_scan.scan(
            "",  # pragma: no mutate -- gitleaks git ignores stdin
            "the staged changes",
            root=pull_request.root(),
            gitleaks=pull_request.find_tool(pull_request.GITLEAKS),
            visibility=visibility(),
            source=secret_scan.STAGED,
        )
    except SystemExit as exc:
        print(f"pre-commit: {exc}", file=sys.stderr)
        return 1
    return 0
