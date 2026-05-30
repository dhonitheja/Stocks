#!/usr/bin/env bash
# Pre-tool hook: block edits to secrets files and live-mode flips.
# Receives JSON on stdin with file_path and content.

set -euo pipefail

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('input',{}).get('file_path',''))" 2>/dev/null || echo "")
CONTENT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); i=d.get('input',{}); print(i.get('new_string','') or i.get('content',''))" 2>/dev/null || echo "")

# Block edits to secrets files
SECRET_FILES=(".env" ".pem" "credentials.json" "secrets.yaml" "secrets.yml")
for sf in "${SECRET_FILES[@]}"; do
    if [[ "$FILE" == *"$sf" ]]; then
        echo "BLOCKED: edits to secrets file '$FILE' are not allowed" >&2
        echo '{"decision":"block","reason":"Blocked by guard-edit.sh: secrets file protection"}'
        exit 0
    fi
done

# Block hardcoding live mode
if echo "$CONTENT" | grep -qE 'TRADING_MODE\s*=\s*["\x27]?live'; then
    echo "BLOCKED: hardcoding TRADING_MODE=live in source is not allowed" >&2
    echo '{"decision":"block","reason":"Blocked by guard-edit.sh: do not hardcode live trading mode"}'
    exit 0
fi

# Block committing actual key values (rough heuristic: long alphanumeric strings in key fields)
if echo "$CONTENT" | grep -qiE '(API_KEY|SECRET|TOKEN|PASSWORD)\s*=\s*[A-Za-z0-9_\-]{20,}'; then
    echo "WARNING: possible secret value in edit — ensure this is a placeholder, not a real key" >&2
fi

echo '{"decision":"allow"}'
