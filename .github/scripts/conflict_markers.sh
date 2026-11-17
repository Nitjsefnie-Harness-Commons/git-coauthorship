#!/usr/bin/env bash
set -uo pipefail
found=0
git grep -nI -E '^(<{7}( |$)|>{7}( |$)|={7}$)' -- . || found=$?
if [ "$found" -gt 1 ]; then
  echo "git grep exited $found, so no marker was looked for."
  exit "$found"
fi
if [ "$found" -eq 0 ]; then
  echo "A merge-conflict marker is committed. Resolve the conflict, rebase onto the target branch and push again."
  exit 1
fi
