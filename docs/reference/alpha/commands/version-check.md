---
description: Read-only comparison of the installed build on each host.
status: stable
tags: [alpha]
title: Version check
type: Reference
---

# Version check

`version-check [--host NAME ...] [--expect SHA|latest] [--json]` compares the installed build on each host, read-only.
For each host it runs `$HOME/.local/bin/tbhbdev --version` over `ssh -n -o BatchMode=yes -o ConnectTimeout=5` (directly on this host) and prints `<host> <sha-or-unknown> <state>`, where the state is `ok`, `stale`, `unknown` or `unreachable`.
`--json` prints a list of objects with `host`, `version` and `state`.
Hosts come from `--host` or the names in `[harness] hosts` of the user config, and exits 2 with neither source.
`--expect SHA` compares every host to that 40-hex commit, `--expect latest` to the head of origin's main, and with neither the reference is the build most hosts run (the first host's on a tie).
It exits 0 when every host is `ok`, 1 when any is stale or unknown, 2 for a usage error, and 3 when a host is unreachable and none is stale (or `latest` is unavailable).
The build is the commit in the `direct_url.json` of the install, else the 40-hex `COMMIT` of `tbhbdev/_build.py`, a stamp the repository never commits and that nothing writes yet, so a wheel install without one reports `unknown`.
