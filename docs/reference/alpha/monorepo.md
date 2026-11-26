---
description: Mapping a public repository's files at one commit and importing them into the monorepo.
generated:
  - role: generated
    harness: claude-code
    model: anthropic:claude-sonnet-5-5
    at: "2026-11-26T03:12:28Z"
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
