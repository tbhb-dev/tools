# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha pre-commit-scan`: scan the staged changes for secrets.

The pre-commit and pre-merge-commit shims that `tbhbdev alpha install` writes
run this last, after any chained hook, so it scans the index the commit
records. It runs ``gitleaks git --pre-commit --staged`` under the host's
private gitleaks config, the scan ``tbhbdev alpha pr`` gives pull request text
(see ``tbhbdev.alpha.secret_scan``), with the .gitleaks.toml staged in the
index. gitleaks reads the staged diff with git, which inherits the hook's
``GIT_INDEX_FILE``, so ``git commit -a`` and ``git commit PATH`` scan the index
the commit records. Only added lines count.

A commit hook doesn't call gh, so the visibility is read locally, and only one
case skips the name rules: origin is a GitHub tbhb-dev repository, by
``repo.is_tbhb_dev_url``, whose name is on the host's private repository list
that the pre-push guard reads (see ``tbhbdev.alpha.private_names``). A list
that can't be read, an origin that doesn't parse, another owner, or a name
that merely has a ``-private`` component scans under the full private config.

It fails closed like the pull request scan: a missing or unreadable private
config, one without the canary rule and another, no gitleaks on PATH or through
mise, a gitleaks failure or any finding refuses the commit. A finding is
reported by its rule ID, file and line only.
"""

import sys

from tbhbdev.alpha import private_names, pull_request, secret_scan
from tbhbdev.alpha.repo import git, is_tbhb_dev_url


def visibility() -> str:
    """``PRIVATE`` for a tbhb-dev origin on the host's private list, else ""."""
    code, url = git("config", "--get", "remote.origin.url")
    if code != 0 or not is_tbhb_dev_url(url):
        return ""
    name = private_names.repository_name(url)
    try:
        host = private_names.host_names()
    except private_names.NameListError:
        return ""
    listed = {item.lower() for item in host.private} | host.path_only
    return secret_scan.PRIVATE if name in listed else ""


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
