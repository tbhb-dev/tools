"""The path map: what happens to each file of a source commit.

The map is TOML, not JSON, because a person or agent edits it before the
import and TOML takes comments. Its three tables are keyed by the source path,
so each source file appears once:

    [copy]      "src/a.py" = "factory/x/src/a.py"
    [rewrite.p] dest = "factory/x/AGENTS.md", replace = [["old text", "new"]]
    [drop]      "uv.lock" = "root configuration"
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

import tomlkit

from tbhbdev.alpha.monorepo.source import MonorepoError, contents

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator

    from tbhbdev.alpha.monorepo.source import Blob

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
class Allow:
    path: str
    reason: str
    pattern: re.Pattern[str]
    expression: str
    kind: str

    def as_table(self) -> dict[str, str]:
        return {
            "path": self.path,
            self.kind: self.expression,
            "reason": self.reason,
        }


@dataclass
class PathMap:
    repo: str
    commit: str
    dest: str
    copy: dict[str, str] = field(default_factory=dict[str, str])
    rewrite: dict[str, Rewrite] = field(default_factory=dict[str, Rewrite])
    drop: dict[str, str] = field(default_factory=dict[str, str])
    allow: list[Allow] = field(default_factory=list[Allow])

    def sources(self) -> set[str]:
        return {*self.copy, *self.rewrite, *self.drop}

    def writes(self) -> dict[str, tuple[str, Rewrite | None]]:
        """Each written source with its destination and rewrite, if any."""
        found: dict[str, tuple[str, Rewrite | None]] = {
            src: (dest, None) for src, dest in self.copy.items()
        }
        found.update({src: (r.dest, r) for src, r in self.rewrite.items()})
        return found

    def gaps(self, listing: Collection[str]) -> list[str]:
        """The disagreements between the map and the source's files."""
        found, known = set(listing), self.sources()
        return [
            *(f"{s}: source file is not in the map" for s in sorted(found - known)),
            *(f"{s}: in the map but not in the source" for s in sorted(known - found)),
        ]


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
            "allow": [entry.as_table() for entry in made.allow],
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


def table(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        message = f"map key {key!r} must be a table"
        raise MonorepoError(message)
    return cast("dict[str, object]", value)


def text(value: object, name: str) -> str:
    if not isinstance(value, str):
        message = f"map value {name} must be a string"
        raise MonorepoError(message)
    return value


PAIR = 2


def pairs(value: object, name: str) -> list[tuple[str, str]]:
    if not isinstance(value, list) or not all(
        isinstance(item, list)
        and len(item) == PAIR
        and all(isinstance(t, str) for t in cast("list[object]", item))
        for item in cast("list[object]", value)
    ):
        message = f"map value {name} must be a list of [old, new] strings"
        raise MonorepoError(message)
    return [(old, new) for old, new in cast("list[list[str]]", value)]


def allow_entries(value: object) -> list[Allow]:
    """Read the exceptions that apply to findings in destination files.

    Raises:
        MonorepoError: An entry is malformed or has an invalid regex.
    """
    if not isinstance(value, list):
        message = "map key 'allow' must be a list of tables"
        raise MonorepoError(message)
    found: list[Allow] = []
    for index, entry in enumerate(cast("list[object]", value), 1):
        label = f"allow entry {index}"
        if not isinstance(entry, dict):
            message = f"{label} must be a table"
            raise MonorepoError(message)
        body = cast("dict[str, object]", entry)
        kind = body.keys() - {"path", "reason"}
        if kind not in ({"literal"}, {"regex"}):
            message = (
                f"{label} must have path, reason and exactly one of literal or regex"
            )
            raise MonorepoError(message)
        key = kind.pop()
        path = check_path(text(body.get("path"), f"{label} path"))
        reason = text(body.get("reason"), f"{label} reason")
        expression = text(body[key], f"{label} {key}")
        if not reason.strip() or reason.splitlines()[0] != reason or not expression:
            message = f"{label} must have a one-line reason and a nonempty {key}"
            raise MonorepoError(message)
        try:
            pattern = re.compile(
                re.escape(expression) if key == "literal" else expression
            )
        except (re.error, OverflowError, RecursionError) as exc:
            message = f"{label} has an invalid regex: {exc}"
            raise MonorepoError(message) from exc
        found.append(Allow(path, reason.strip(), pattern, expression, key))
    return found


def load(content: str) -> PathMap:
    """Parse and check a map.

    Raises:
        MonorepoError: The map isn't valid TOML, has a malformed entry, lists
            a source twice, writes a path twice or writes outside ``dest``.
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        message = f"map is not valid TOML: {exc}"
        raise MonorepoError(message) from exc
    dest = check_path(text(data.get("dest"), "dest"))
    made = PathMap(
        text(data.get("repo"), "repo"), text(data.get("commit"), "commit"), dest
    )
    if made.repo.count("/") != 1:
        message = f"map repo {made.repo!r} must be OWNER/NAME"
        raise MonorepoError(message)
    made.copy = {k: text(v, k) for k, v in table(data, "copy").items()}
    made.allow = allow_entries(data.get("allow", []))
    made.drop = {k: text(v, k) for k, v in table(data, "drop").items()}
    rewrite = table(data, "rewrite")
    for src in rewrite:
        body = table(rewrite, src)
        made.rewrite[src] = Rewrite(
            text(body.get("dest"), f"{src} dest"), pairs(body.get("replace", []), src)
        )
    if len(made.sources()) != len(made.copy) + len(made.rewrite) + len(made.drop):
        message = "map lists one source in more than one table"
        raise MonorepoError(message)
    written = [path for path, _ in made.writes().values()]
    if len(set(written)) != len(written):
        message = "map writes one path twice"
        raise MonorepoError(message)
    for path in written:
        check_path(path, under=dest)
    return made


def rewrite_bytes(src: str, data: bytes, entry: Rewrite | None) -> bytes:
    """Apply the map's replacements to one source file.

    Raises:
        MonorepoError: A replacement's old text isn't in its file.
    """
    for old, new in entry.replace if entry else ():
        if old.encode() not in data:
            message = f"{src}: replacement {old!r} isn't in the file"
            raise MonorepoError(message)
        data = data.replace(old.encode(), new.encode())
    return data


def outputs(
    clone: str, listing: dict[str, Blob], made: PathMap
) -> Iterator[tuple[str, str, str, bytes]]:
    """Each written file as ``(source, dest, mode, bytes)``, bytes as rewritten.

    Raises:
        MonorepoError: A replacement's old text isn't in its file.
    """
    writes = made.writes()
    srcs = sorted(writes)
    for src, data in zip(
        srcs, contents(clone, [listing[s].oid for s in srcs]), strict=True
    ):
        dest, entry = writes[src]
        yield src, dest, listing[src].mode, rewrite_bytes(src, data, entry)
