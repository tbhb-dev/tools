---
base: main
draft: true
labels:
  - enhancement
reviewers:
  - tbhb
assignees:
  - "@me"
---

# feat(cli): add the stamp subcommand

## Why

Running `tool stamp` on a tree with a file that lacks write permission stops partway. The command leaves some files stamped and others not, and prints nothing that says which. See [the issue](https://github.com/tbhb-dev/tools/issues/12).

## Behavior change

The command now checks every target before it writes one. If a file lacks write permission, the command exits with code 1 and lists the blocked paths. A run that passes the check stamps every file.

## Verification

Ran the new unit tests, then tried to stamp a tree with a file that lacked write permission:

```console
$ go test ./internal/stamp/...
ok  	github.com/tbhb-dev/tools/internal/stamp	0.412s
$ tool stamp ./fixture
error: 1 file is read-only: fixture/a.txt
```

No file in the tree changed after the failure.
