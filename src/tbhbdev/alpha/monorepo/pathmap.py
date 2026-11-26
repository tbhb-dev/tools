"""The path map: what happens to each file of a source commit.

The map is TOML, not JSON, because a person or agent edits it before the
import and TOML takes comments. Its three tables are keyed by the source path,
so each source file appears once:

    [copy]      "src/a.py" = "factory/x/src/a.py"
    [rewrite.p] dest = "factory/x/AGENTS.md", replace = [["old text", "new"]]
    [drop]      "uv.lock" = "root configuration"
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

import tomlkit

from tbhbdev.alpha.monorepo.source import MonorepoError

# Root configuration that the shared roots of the monorepo replace, from the
# root of tbhb-dev/tools. A pattern matches the whole path, and `*` stays
# within one folder.
ROOT_CONFIG = (
    ".bazel*",
    ".cspell*",
    ".editorconfig*",
    ".gitattributes",
    ".gitignore",
    ".gitleaks.toml",
    ".github/CODEOWNERS",
    ".github/actionlint.yaml",
    ".github/renovate.json*",
    ".github/rulesets/*",
    ".github/workflows/*",
    ".github/zizmor.yml",
    ".pre-commit-config.yaml",
    ".python-version",
    ".rumdl.toml",
    ".ryl*",
    ".vale*",
    ".vale/**",
    "LICENSE",
    "LICENSES/**",
    "MODULE.bazel*",
    "REPO.bazel",
    "REUSE.toml",
    "mise.*",
    "preset.bazelrc",
    "renovate.json*",
    "size-check.sh",
    "tombi.toml",
    "uv.lock",
)
HEADER = "# Edit before `tbhbdev alpha monorepo import`. See the monorepo docs.\n"


@dataclass
class Rewrite:
    dest: str
    replace: list[tuple[str, str]]


@dataclass
class PathMap:
    repo: str
    commit: str
    dest: str
    copy: dict[str, str] = field(default_factory=dict[str, str])
    rewrite: dict[str, Rewrite] = field(default_factory=dict[str, Rewrite])
    drop: dict[str, str] = field(default_factory=dict[str, str])


def build(repo: str, commit: str, dest: str, paths: list[str]) -> PathMap:
    """The default map: copy under ``dest``, drop the root configuration."""
    made = PathMap(repo, commit, dest)
    for path in sorted(paths):
        pattern = next(
            (p for p in ROOT_CONFIG if PurePosixPath(path).full_match(p)), None
        )
        if pattern:
            made.drop[path] = f"root configuration ({pattern})"
        else:
            made.copy[path] = f"{dest}/{path}"
    return made


def dump(made: PathMap) -> str:
    rewrite = {
        src: {"dest": r.dest, "replace": [list(pair) for pair in r.replace]}
        for src, r in made.rewrite.items()
    }
    return HEADER + tomlkit.dumps(
        {
            "repo": made.repo,
            "commit": made.commit,
            "dest": made.dest,
            "copy": made.copy,
            "rewrite": rewrite,
            "drop": made.drop,
        }
    )


def check_path(path: str, *, under: str | None = None) -> str:
    """Refuse a path that is absolute, unnormalized, in .git, or outside ``under``."""
    parts = PurePosixPath(path)
    if (
        not path
        or str(parts) != path
        or parts.is_absolute()
        or {".", "..", ".git"} & {p.lower() for p in parts.parts}
        or (under is not None and not parts.is_relative_to(under))
    ):
        where = f" under {under}" if under else ""
        message = f"bad path {path!r}: want a clean relative path{where}"
        raise MonorepoError(message)
    return path
