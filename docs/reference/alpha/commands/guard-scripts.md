---
description: The hook that keeps operator scripts in a fixed directory with fixed names.
status: stable
tags: [alpha]
title: Guard scripts
type: Reference
---

# Guard scripts

`guard-scripts` is a PreToolUse hook that keeps scripts for the operator in `~/.local/libexec/tbhb/`.
It reads the common ways an agent writes a file, so it misses the rest.
It denies a script written anywhere else under the home directory, `/tmp` or `/private/tmp`, and any script in a drafts repository.
Git work trees, this session's scratchpad and a run-scoped `$TMPDIR` pass.
Name each file `YYYY-MM-DD-<owner>-<slug>[.vN].sh` or `YYYY-MM-DD-<owner>-<slug>[.vN].py`.
The guard refuses a write to an existing file.
Give each revision a new name.
Both suffixes and any content starting with `#!` count as scripts.
When the hook can read the content, a new `.py` file there must start with exactly `#!/usr/bin/env -S python3 -I`.
Make it executable with `chmod 0755`.
Known gaps include unknown content (such as `echo` output) and the executable bit, which the hook can't check at write time.
Run logs go in `~/.local/state/tbhb/<same name>/`.
