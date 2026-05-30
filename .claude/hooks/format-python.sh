#!/usr/bin/env bash
# Post-tool hook: auto-format Python files after Write/Edit.
# Receives JSON on stdin with file_path.

set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('input',{}).get('file_path',''))" 2>/dev/null || echo "")

# Only act on Python files
if [[ "$FILE" != *.py ]]; then
    exit 0
fi

# Run black if available (silent — don't fail the edit if formatter missing)
if command -v black &>/dev/null; then
    black --quiet "$FILE" 2>/dev/null || true
elif python3 -m black --version &>/dev/null 2>&1; then
    python3 -m black --quiet "$FILE" 2>/dev/null || true
fi

# Run ruff --fix if available
if command -v ruff &>/dev/null; then
    ruff check --fix --quiet "$FILE" 2>/dev/null || true
elif python3 -m ruff --version &>/dev/null 2>&1; then
    python3 -m ruff check --fix --quiet "$FILE" 2>/dev/null || true
fi

exit 0
