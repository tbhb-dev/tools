# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""The example repo-settings.toml the repo bootstrap tests share."""

EXAMPLE = """
[defaults]
allow_squash_merge = true
allow_merge_commit = false
allow_rebase_merge = false
squash_merge_commit_title = "PR_TITLE"
squash_merge_commit_message = "BLANK"
delete_branch_on_merge = true
has_issues = false
has_wiki = false
has_projects = false

[repos."example-org/example-repo"]
has_issues = true
"""

# What [defaults] of EXAMPLE parses to.
DEFAULTS = {
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
    "squash_merge_commit_title": "PR_TITLE",
    "squash_merge_commit_message": "BLANK",
    "delete_branch_on_merge": True,
    "has_issues": False,
    "has_wiki": False,
    "has_projects": False,
}
