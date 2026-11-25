# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Fake command-line tools for the alpha tests."""

import os
import shlex
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def pythonpath(src: Path) -> str:
    """PYTHONPATH for a child Python: src, then the one the tests run with.

    Under Bazel the dependencies come through PYTHONPATH, not a virtualenv.
    """
    return os.pathsep.join(filter(None, [str(src), os.environ.get("PYTHONPATH")]))


def write_fake(script: Path, body: str) -> None:
    """A fake tool: a /bin/sh stub that runs ``body`` (saved beside it) in Python.

    Not a ``#!<python>`` shebang: under Bazel on Linux the interpreter path can
    pass the kernel's shebang length limit, or be a script itself, and exec
    then fails with ENOEXEC.
    """
    source = script.with_name(script.name + ".py")
    source.write_text(body)
    python, path = shlex.quote(sys.executable), shlex.quote(str(source))
    script.write_text(f'#!/bin/sh\nexec {python} {path} "$@"\n')
    script.chmod(0o755)


# tbhb_rules' pinned git is a /bin/sh wrapper that finds its archive with
# readlink and dirname and checks the kernel with uname, so a PATH that
# holds git must hold these too.
GIT_TOOLS = ("git", "readlink", "dirname", "uname")
PR_ORIGIN = "git@github.com:tbhb-dev/example.git"


def link_git(directory: Path) -> None:
    """A new directory for PATH that holds git and the tools it runs."""
    directory.mkdir()
    for name in GIT_TOOLS:
        (directory / name).symlink_to(shutil.which(name) or name)


@pytest.fixture(scope="session")
def pr_git_baseline(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Seed Git metadata once for pull request and review tests to copy."""
    home = tmp_path_factory.mktemp("pr-git-baseline")
    root = home / "repo"
    root.mkdir()
    git_dir = home / "bin"
    link_git(git_dir)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "xdg"),
        PATH=str(git_dir),
        GIT_AUTHOR_NAME="T",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="T",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    for argv in (
        ("init", "-q", "-b", "main", "--template="),
        ("config", "commit.gpgsign", "false"),
        ("remote", "add", "origin", PR_ORIGIN),
        ("commit", "--allow-empty", "-q", "-m", "init"),
        ("update-ref", "refs/remotes/origin/main", "HEAD"),
        ("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"),
    ):
        subprocess.run(
            ["git", *argv], cwd=root, env=env, capture_output=True, check=True
        )
    return root / ".git"


# A stand-in for the host's private gitleaks config: it extends the committed
# config and defines the canary rule that marks it as the private one.
PRIVATE_CONFIG = """[extend]
path = ".gitleaks.toml"

[[rules]]
id = "tbhb-private-canary"
description = "Test canary"
regex = '''test-canary-[0-9a-f]{16}'''
"""

# One repository rule, which no scan keeps, and one host rule, which a scan of a
# private repository drops.
RULES_BEYOND_ONE = """
[[rules]]
id = "private-repo-1"
regex = '''repo-x'''

[[rules]]
id = "tailnet-host-1"
regex = '''host-x'''
"""


def write_private_config(home: Path, text: str = PRIVATE_CONFIG) -> Path:
    """Write the private gitleaks config under HOME and return its path."""
    path = home / ".config" / "gitleaks" / "private.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
