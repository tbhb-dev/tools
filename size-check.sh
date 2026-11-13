#!/usr/bin/env bash
# Portable Bash plus find/wc and Git; sizes are logical bytes, not allocated blocks.
set -euo pipefail
cd -- "$(dirname -- "$0")"
git rev-parse --git-dir >/dev/null
# Each listing goes to a file first, so a failing find or git fails the check
# instead of hiding behind a process substitution.
list=$(mktemp "${TMPDIR:-/tmp}/size-check.XXXXXX")
trap 'rm -f -- "$list"' EXIT
failed=0
find . -path './.git' -prune -o -type f -size +20000000c -print0 >"$list"
while IFS= read -r -d '' path; do
  path=${path#./}
  if git ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
    : # Tracked files must obey the limit even if an ignore rule was added later.
  elif git check-ignore -q -- "$path"; then
    continue
  fi
  size=$(wc -c <"$path")
  printf 'Over 20 MB: %s (%s bytes)\n' "$path" "$size" >&2
  failed=1
done <"$list"
# Check staged blobs too: the working copy can differ from the commit candidate.
git ls-files --stage -z >"$list"
while IFS= read -r -d '' record; do
  metadata=${record%%$'\t'*}
  path=${record#*$'\t'}
  read -r _ oid _ <<<"$metadata"
  size=$(git cat-file -s "$oid")
  if ((size > 20000000)); then
    printf 'Over 20 MB in index: %s (%s bytes)\n' "$path" "$size" >&2
    failed=1
  fi
done <"$list"
if ((failed == 0)); then
  printf 'PASS: no non-ignored or tracked file exceeds 20000000 bytes.\n'
fi
exit "$failed"
