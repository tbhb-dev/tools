# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Public prose check of text before a tool posts it.

``check_prose`` is the stable entry point for tools that post agent-written
text. It runs vale, cspell and rumdl the way ``tbhbdev alpha pr check`` does,
through ``tbhbdev.alpha.pull_request.lint``, so one path decides what prose may
be posted.
"""

import contextlib
from typing import TYPE_CHECKING

from tbhbdev.alpha import pull_request

if TYPE_CHECKING:
    from pathlib import Path

# The draft path: every glob in .vale.ini, .cspell.jsonc and .rumdl.toml that
# matters for prose matches it, and ``pr check`` lints the same path.
PATH = pull_request.DRAFT


class ProseRefused(RuntimeError):  # noqa: N818 - the public name callers import
    """The text has a finding, or a linter or its config is missing."""


def check_prose(text: str, what: str, *, root: Path) -> None:
    """Refuse text that vale, cspell or rumdl flag under the repository's configs.

    Args:
        text: The prose to check, as Markdown.
        what: What the text is, for the refusal message.
        root: The repository root, which holds the lint configs. The check runs
            with it as the working directory, because ``lint`` finds the root
            from there.

    Raises:
        ProseRefused: A linter found a problem or couldn't run, a missing or
            non-executable linter included. The message is ``what`` plus one
            ``line N: <rule text>`` line per finding, or ``what`` and the OS
            reason, never the text.
    """
    try:
        with contextlib.chdir(root):
            problems = pull_request.lint(text.splitlines(), 1, path=PATH)
    except SystemExit as exc:
        message = f"{what}\n{exc.code}"
        raise ProseRefused(message) from None
    except OSError as exc:
        message = f"cannot check {what} for prose: a linter cannot run ({exc.strerror})"
        raise ProseRefused(message) from None
    if problems:
        lines = [f"line {number}: {rule}" for number, rule in problems]
        message = "\n".join([what, *lines])
        raise ProseRefused(message)
