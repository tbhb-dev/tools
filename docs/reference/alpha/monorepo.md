---
description: Mapping, importing and verifying a public repository's files at one commit.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:5e455a763821b0df
    at: "2026-11-26T04:59:30Z"
status: stable
tags: [alpha, monorepo]
title: Monorepo import
type: Reference
---

# Monorepo import

`tbhbdev alpha monorepo` moves one public-origin repository into the monorepo at one commit. It stops short of a push or a pull request. Private-origin repositories are unsupported.

## Path map

`map CLONE COMMIT --dest FOLDER` lists every file of the source clone at the commit and writes the path map to stdout or to `-o FILE`. It records the source repository, the full commit hash, and the monorepo folder for the project. `--repo OWNER/NAME` gives the source repository. Without it the command reads the GitHub address of the clone's `origin` and stops when it finds none. A submodule or a path that isn't UTF-8 also stops the command.

The map is TOML. A person or agent edits it before the import, and TOML takes comments. Three tables, each keyed by the source path, list each source file once:

- `[copy]` maps a source path to its new path. The default map sends every file to `FOLDER/<source path>`.
- `[rewrite."<source path>"]` has a `dest` and a `replace` list of `[old, new]` text pairs. Each pair replaces its old text in the source file to make the new file. Move a file here from `[copy]` when it calls, pins, or imports an old repository. Examples include a workflow call, a Renovate preset, a Bazel override, a Go import path, and an agent instruction file.
- `[drop]` maps a source path to the reason the import leaves it out.

The default map drops the root configuration that the monorepo's shared roots replace, and gives the matching pattern as the reason. The patterns come from the root of tbhb-dev/tools and sit in the `ROOT_CONFIG` table of the map module. They cover the Bazel and Mise files, lock files, lint, spelling, prose, and editor settings, the Renovate files, the license files, `REUSE.toml`, and the repository's workflows, rulesets, `CODEOWNERS`, and workflow linter settings. A pattern matches the whole path, and `*` stays within one folder. The default map keeps everything else, including the agent instruction files, `pyproject.toml`, and the build files, which a person reviews.

Every destination is a clean relative path under the folder, with no `..` and no `.git`.

## Import

`import CLONE@COMMIT --map MAP --into MONOREPO` writes the copied and rewritten files of the map into the monorepo clone and makes one commit. Pushing and opening the pull request stay with the caller. `CLONE@COMMIT` splits at its last `@`, and the map must record the same full commit.

Before it writes anything, the command refuses when any of these hold:

- The map misses a source file, lists one the commit lacks, doesn't parse, lists a source twice, or writes a path twice.
- The monorepo clone has uncommitted changes.
- A destination file already exists in the clone.
- The commit subject would pass 72 characters.

An import never overwrites a file. `--into` can be any folder of the clone, and destinations are relative to the clone's root. It also refuses a destination that leads outside the map's folder through a symbolic link in the clone. Git runs from the clone's root, not from the folder `--into` names. When a write, `git add`, or `git commit` fails, it removes the files from the index and removes the files and folders it created, which leaves the clone as it was. Files keep the source's bytes and executable bit, and a symbolic link in the source stays a symbolic link with the same target. A `replace` pair whose old text isn't in the file is an error, because a stale map would otherwise copy the file unchanged.

Subjects read `feat(FOLDER): import REPO at SHORT`, with the repository name and the first 7 characters of the hash. Its body gives the source repository and the full commit hash, then counts the copied, rewritten, and dropped files. The import makes a plain `git commit`, so any signing comes from the monorepo clone's Git config and the tool doesn't check the signature. Hooks of the monorepo clone run on the commit, which has no parent in the source repository. Exit code 0 is success. Exit code 2 is a refusal or a Git error.

## Verify

`verify CLONE@COMMIT --map MAP --tree MONOREPO` is the command that the monorepo's required `import` job runs. It reads the source at the commit and the files in the tree. Its output is one line for each problem, then `verify: N problems`. Exit code 0 means no problems, 1 means problems, and 2 means the command couldn't run.

It checks that:

- The map accounts for every source file and lists none that the commit lacks.
- Every file under the imported folder is a destination produced by the map. Unexpected files, retained drops, and old paths left after a move fail the check.
- The imported folder and its parents contain no symbolic links. The check treats directory links within the folder as files and compares their targets and kind with the map. It doesn't follow them.
- Each copied file exists in the tree with the source's bytes, executable bit, and link or file kind.
- Each rewritten file equals the source with the replacements of the map applied.
- No copied or rewritten file has an executable reference to an old repository.

After reporting gaps in the map, stale replacements, or paths it can't inspect, verification continues through the remaining files under the imported folder.

An executable reference is a line of a file that isn't prose. Markdown, reStructuredText, AsciiDoc, and plain text files count as prose, since prose that mentions an old repository is a dated record. A line matches when it has one of these:

- A workflow `uses:` line for any repository of the source's owner.
- A Renovate `github>` or `local>` preset from any repository of the owner.
- A clone address of the source repository, `github.com/OWNER/NAME` or `github.com:OWNER/NAME`. This covers Bazel overrides, scripts, and Go import paths on GitHub.
- A module path from one of the source's `go.mod` files, which covers a vanity import path.

Each finding reads `DEST:LINE: RULE`, and a line reports the first rule it matches. Symbolic links aren't scanned. The check can't find a reference to a repository of another owner, because it has no list of the other old repositories.
