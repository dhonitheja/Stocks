#!/usr/bin/env bash
# Pre-tool hook: block destructive shell commands in trading repo.
# Receives the command string on stdin as JSON: {"tool":"Bash","input":{"command":"..."}}

set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('input',{}).get('command',''))" 2>/dev/null || echo "")

# Patterns that must never run unattended in this repo
BLOCKED_PATTERNS=(
    "TRADING_MODE=live"
    "rm -rf /"
    "git push --force"
    "git push -f"
    "DROP TABLE"
    "DELETE FROM.*WHERE.*1=1"
    "liquidate_all"
    "--i-understand-the-risk"   # must be human-typed, not scripted
)

for pattern in "${BLOCKED_PATTERNS[@]}"; do
    if echo "$CMD" | grep -qiE "$pattern"; then
        echo "BLOCKED: command matches safety pattern '$pattern'" >&2
        echo '{"decision":"block","reason":"Blocked by guard-bash.sh safety hook: '"$pattern"'"}'
        exit 0
    fi
done

# Warn (but allow) on secrets-adjacent commands
WARN_PATTERNS=(".env" "API_KEY" "SECRET" "TOKEN" "PASSWORD")
for pattern in "${WARN_PATTERNS[@]}"; do
    if echo "$CMD" | grep -qiE "$pattern"; then
        echo "WARNING: command references sensitive pattern '$pattern' — verify it does not log/expose secrets" >&2
        break
    fi
done

echo '{"decision":"allow"}'
