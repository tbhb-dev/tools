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

Running `tool stamp` on a tree with a file that denies writes stops partway. The command leaves some files stamped and omits the list of paths it skipped. See [the issue](https://github.com/tbhb-dev/tools/issues/12).

## Behavior change

The command now checks every target before it writes one. A file that denies writes fails the whole run with an exit code of 1 and a list of the blocked paths. After the check passes, the command stamps every file.

## Verification

Ran the new unit tests and tried to stamp a tree with one file that denies writes.

```console
$ go test ./internal/stamp/...
ok  	github.com/tbhb-dev/tools/internal/stamp	0.412s
$ tool stamp ./fixture
error: 1 file is read-only: fixture/a.txt
```

No file in the tree changed after the failure.
