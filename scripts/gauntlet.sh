#!/usr/bin/env bash
# Local gauntlet: run before every push. CI repeats all but check-private.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
uv lock --check
uv run ruff check . --fix
uv run ruff format .
uv run mypy src
for f in src/ultra/static/*.js; do node --check "$f"; done
uv run pytest -q
scripts/check-private.sh
echo "gauntlet: all green"
