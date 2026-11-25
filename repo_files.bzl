"""Gitignored files that can sit in a Bazel package."""

REPO_FILE_EXCLUDES = [
    ".DS_Store",
    "**/.DS_Store",
    ".bazelrc.user",
    ".coverage*",
    ".env*",
    ".github/PULL_REQUEST_DRAFT.md",
    "coverage.*",
    "mise.local.toml",
]
