#!/usr/bin/env python3
"""PreToolUse safety hook for the trading repo (cross-platform). A guard rail, not a hard gate.

Replaces guard-bash.sh and guard-edit.sh, which were never registered and read the wrong payload key
(`input` instead of `tool_input`), so they could not have blocked anything. protect-env.ps1 is kept
as a second, Windows-only layer for .env.
Claude Code sends {"tool_name": ..., "tool_input": {...}} on stdin; exit 2 blocks the call and
stderr is shown to the model. Unexpected errors fail CLOSED (exit 2). Unparseable stdin passes.

Blocks:
  * reading/searching/editing real secret files (.env, .env.*, *.pem, *.key, secret config files);
    .env.example is allowed
  * shell commands that read .env, set TRADING_MODE to live, pass --i-understand-the-risk,
    force-push (not --force-with-lease) or run destructive SQL / rm -rf /
  * code edits that hard-code TRADING_MODE=live (docs and markdown are exempt)

This is regex heuristics and can be bypassed by determined obfuscation (e.g. building strings at
runtime). For real enforcement also use the permissions deny list in Claude Code settings.
"""
import json
import os
import re
import sys

BLOCK = 2


def block(msg):
    print(f"BLOCKED by .claude/hooks/guard.py: {msg}", file=sys.stderr)
    sys.exit(BLOCK)


def is_secret_path(path):
    name = os.path.basename(str(path or "")).lower()
    if not name or name == ".env.example":
        return False
    base, ext = os.path.splitext(name)
    if name == ".env" or name.startswith(".env.") or ext in (".pem", ".key"):
        return True
    if name == "credentials.json":
        return True
    # "secret" in a data/config filename, but not source files like secrets_helper.py
    return "secret" in name and ext not in (".py", ".md", ".rst", ".js", ".ts")


# .env, .env.local, .env.production ... but not .env.example
ENVTOK = r"\.env(?!\.example(?![\w]))(?:\.[\w-]+)*(?![\w])"
LIVE = r"TRADING_MODE[\"'\]\s]*[=:][\"'\s]*live"

BASH_BLOCKED = [
    (LIVE, "TRADING_MODE=live must be set by a human, not scripted"),
    (r"--i-understand-the-risk", "live-trading acknowledgement must be typed by a human"),
    (r"git\s+push\b[^;&|]*\s(--force(?![-\w])|-f(?![-\w]))", "force-push is not allowed"),
    (r"git\s+push\b[^;&|]*\s\+[\w/.-]+", "force-push via +refspec is not allowed"),
    (r"rm\s+-rf?\s+/(\s|$)", "destructive rm"),
    (r"DROP\s+TABLE", "destructive SQL"),
    (r"DELETE\s+FROM[^;]*WHERE\s+1\s*=\s*1", "destructive SQL"),
    (r"liquidate_all", "liquidating positions must be a human decision"),
]
READS_ENV = [
    rf"\b(cat|type|head|tail|less|more|bat|nl|strings|xxd|od|cp|mv|scp|base64|tee)\s+[^|;&]*{ENVTOK}",
    rf"Get-Content\s+[^|;&]*{ENVTOK}",
    rf"\b(grep|rg|egrep|fgrep|findstr|Select-String|awk|sed)\b[^|;&]*{ENVTOK}",
    rf"<\s*[\"']?[^\s|;&]*{ENVTOK}",
    rf"\b(source|\.)\s+[\"']?[^\s|;&]*{ENVTOK}",
    rf"open\([^)]*{ENVTOK}",
    r"\b(printenv|env)\b\s*($|[|;&])",
]
# Commands whose quoted text is a message, not something executed.
MESSAGE_CMDS = re.compile(r"^\s*(git\s+commit|gh\s+(pr|issue)\b|echo\b|printf\b)")
LIVE_IN_CONTENT = re.compile(LIVE, re.I)
DOC_EXT = (".md", ".rst", ".txt")


def scrub_quotes(cmd):
    return re.sub(r"\"[^\"]*\"|'[^']*'", '""', cmd)


def edit_text(ti):
    parts = [str(ti.get(k, "")) for k in ("content", "new_string", "new_source")]
    parts += [str(e.get("new_string", "")) for e in (ti.get("edits") or []) if isinstance(e, dict)]
    return " ".join(parts)


def check(payload):
    tool = payload.get("tool_name") or payload.get("tool") or ""
    ti = payload.get("tool_input") or payload.get("input") or {}
    if not isinstance(ti, dict):
        ti = {}

    if tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path")
        if is_secret_path(path):
            block(f"{os.path.basename(path)} is a secrets file")
        if tool != "Read" and not str(path or "").lower().endswith(DOC_EXT):
            if LIVE_IN_CONTENT.search(edit_text(ti)):
                block("do not hard-code TRADING_MODE=live in source")
    elif tool == "Grep":
        if is_secret_path(ti.get("path")) or re.search(r"\.env$", str(ti.get("glob") or "")):
            block(".env cannot be searched")
    elif tool == "Bash":
        cmd = str(ti.get("command") or "")
        scan = scrub_quotes(cmd) if MESSAGE_CMDS.match(cmd) else cmd
        for pat, why in BASH_BLOCKED:
            if re.search(pat, scan, re.I):
                block(why)
        for pat in READS_ENV:
            if re.search(pat, scan, re.I):
                block("this command would read or dump .env / environment secrets")


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return  # not our payload; do not block
    if not isinstance(payload, dict):
        return
    try:
        check(payload)
    except SystemExit:
        raise
    except Exception as exc:  # fail closed on our own bugs
        block(f"hook error ({type(exc).__name__}); failing closed")


if __name__ == "__main__":
    main()
