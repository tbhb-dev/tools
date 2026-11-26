---
description: Mapping, importing and verifying a public repository's files at one commit.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.2-sol
    session: sha-256-64:f0c981acb10c8642
    at: "2026-11-26T15:32:16Z"
status: stable
tags: [alpha, monorepo]
title: Monorepo import
type: Reference
---

# Monorepo import

`tbhbdev alpha monorepo` moves one public-origin repository into the monorepo at one commit. It stops short of a push or a pull request. Private-origin repositories are unsupported.

## Path map

`map CLONE COMMIT --dest FOLDER` lists every file of the source clone at the commit and writes the path map to stdout or to `-o FILE`. It records the source repository, the full commit hash, and the monorepo folder for the project. `--repo OWNER/NAME` gives the source repository. Without it the command reads the GitHub address of the clone's `origin` and stops when it finds none. A submodule or a path that isn't UTF-8 also stops the command.

Edit the TOML map before importing. Use the source path as the key in each file table.

- `[copy]` maps a source path to its new path. The default map sends every file to `FOLDER/<source path>`.
- `[rewrite."<source path>"]` has a `dest` and a `replace` list of `[old, new]` text pairs. Each pair replaces its old text in the source file to make the new file. Move a file here from `[copy]` when it calls, pins, or imports an old repository. Examples include a workflow call, a Renovate preset, a Bazel override, a Go import path, and an agent instruction file.
- `[drop]` maps a source path to the reason the import leaves it out.

The default map drops the root configuration that the monorepo's shared roots replace, and gives the matching pattern as the reason. The patterns come from the root of tbhb-dev/tools and sit in the `ROOT_CONFIG` table of the map module. They cover the Bazel and Mise files, lock files, lint, spelling, prose, and editor settings, the Renovate files, the license files, `REUSE.toml`, and the repository's workflows, rulesets, `CODEOWNERS`, and workflow linter settings. A pattern matches the whole path, and `*` stays within one folder. The default map keeps everything else, including the agent instruction files, `pyproject.toml`, and the build files, which a person reviews.

Every destination is a clean relative path under the import folder. An optional `allow` list records exceptions for the verifier. Each entry takes a destination `path` glob and a one-line `reason`. Choose `literal` or `regex` for the match.

```toml
[[allow]]
path = "lib/rules/**/tests/*.sh"
literal = "https://github.com/tbhb-dev/rules.git"
reason = "The network test clones the public repository."
```

Place the `[[allow]]` entries independently of the file tables. The glob matches the full monorepo-relative destination path. `*` matches within one folder and `**` can cross folders. A literal or Python regular expression searches the reference text, such as a URL, workflow target, preset, Go path, or Bazel module name. Verification suppresses each matching reference and reports any entry that matches zero findings.

## Import

`import CLONE@COMMIT --map MAP --into MONOREPO` writes the copied and rewritten files of the map into the monorepo clone and makes one commit. Pushing and opening the pull request stay with the caller. `CLONE@COMMIT` splits at its last `@`, and the map must record the same full commit.

Before it writes anything, the command refuses when any of these hold:

- The map misses a source file, lists one the commit lacks, doesn't parse, lists a source twice, or writes a path twice.
- The monorepo clone has uncommitted changes.
- A destination file already exists in the clone.
- The commit subject would pass 72 characters.

An import never overwrites a file. `--into` can be any folder of the clone, and destinations are relative to the clone's root. It also refuses a destination that leads outside the map's folder through a symbolic link in the clone. Git runs from the clone's root, not from the folder `--into` names. When a write, `git add`, or `git commit` fails, it removes the files from the index and removes the files and folders it created, restoring the clone to its prior state. Files keep the source's bytes and executable bit, and a symbolic link in the source stays a symbolic link with the same target. A `replace` pair whose old text isn't in the file is an error, because a stale map would otherwise copy the file unchanged.

Subjects read `feat(FOLDER): import REPO at SHORT`, with the repository name and the first 7 characters of the hash. Its body gives the source repository and the full commit hash, then counts the copied, rewritten, and dropped files. The import makes a plain `git commit`. The monorepo clone's Git config controls signing. Hooks of the monorepo clone run on the commit, which has no parent in the source repository. Exit code 0 is success. Exit code 2 is a refusal or a Git error.

## Verify

`verify CLONE@COMMIT --map MAP --tree MONOREPO` is the command that the monorepo's required `import` job runs. It reads the source at the commit and the files in the tree. Its output is one line for each problem, then `verify: N problems`. Exit code 0 means no problems, 1 means problems, and 2 means the command couldn't run.

The verifier checks the following conditions.

- The map accounts for every source file and lists none that the commit lacks.
- Every file under the imported folder is a destination produced by the map. Unexpected files, retained drops, and old paths left after a move fail the check.
- The imported folder and its parents contain no symbolic links. The check treats directory links within the folder as files and compares their targets and kind with the map. It doesn't follow them.
- Each copied file exists in the tree with the source's bytes, executable bit, and link or file kind.
- Each rewritten file equals the source with the replacements of the map applied.
- Every executable reference to an old repository in a copied or rewritten file has a matching `allow` entry.
- Each `allow` entry matches at least one reference.

After reporting gaps in the map, stale replacements, or paths it can't inspect, verification continues through the remaining files under the imported folder.

The scanner reads regular files outside prose formats such as Markdown, reStructuredText, AsciiDoc, and plain text. It skips comments in JSON5, YAML, TOML, Bazel, and Go files. It also skips `repository`, `homepage`, `documentation`, and `issues` metadata in `galaxy.yml`, `package.json`, `pyproject.toml`, and `Cargo.toml`.

The scanner recognizes these references to the source repository and old repositories in the design's [repository path map](https://github.com/tbhb-dev/wiki/blob/main/designs/monorepo/C-layout-and-ownership.md#where-each-repository-goes).

- Workflow `uses` targets and Renovate `github>` or `local>` presets.
- GitHub clone addresses in HTTPS, SSH, and `git@github.com:OWNER/NAME` form.
- Go paths declared in `go.mod` at the root of the source repository, plus Go references to old GitHub or `go.tbhb.dev` module paths. Paths of imported nested modules can still refer to packages inside those modules.
- Bazel `bazel_dep` calls with an old module name. A matching `local_path_override` in the same module file exempts the dependency when its path resolves inside the monorepo.

A reference finding reads `DEST:LINE: RULE`. Each line reports the first unsuppressed rule. References to repositories the design keeps or adds, such as `tbhb-dev/tbhbdev`, remain allowed.
