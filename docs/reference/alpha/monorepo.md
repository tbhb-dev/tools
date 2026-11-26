---
description: Mapping a public repository's files at one commit for import into the monorepo.
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
