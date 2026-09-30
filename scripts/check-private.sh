#!/usr/bin/env bash
# check-private.sh - fail if any tracked (or staged) file matches a private pattern.
# Patterns live in local/private_patterns.txt, which is git-ignored and never published.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
pat=local/private_patterns.txt
if [[ ! -f $pat ]]; then echo "check-private: $pat missing (skipping; CI relies on gitleaks)"; exit 0; fi
mapfile -t files < <( { git ls-files; git diff --cached --name-only --diff-filter=ACM; git ls-files --others --exclude-standard; } | sort -u | grep -v '^local/' || true)
[[ ${#files[@]} -eq 0 ]] && { echo "check-private: no files"; exit 0; }
hits=$(grep -v '^\s*#' "$pat" | grep -v '^\s*$' | grep -n -i -E -f - "${files[@]}" 2>/dev/null || true)
# Public author credit is intended; allow exactly these lines (local/allow_lines.txt,
# one "file:text" per line, matched literally).
if [[ -n $hits && -f local/allow_lines.txt ]]; then
  hits=$(printf '%s\n' "$hits" | while IFS= read -r h; do
    f=${h%%:*}; rest=${h#*:}; text=${rest#*:}
    grep -qxF "$f:$text" local/allow_lines.txt || printf '%s\n' "$h"
  done)
fi
if [[ -n $hits ]]; then echo "check-private: private data found:"; echo "$hits"; exit 1; fi
echo "check-private: clean (${#files[@]} files)"
