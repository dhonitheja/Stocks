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
    if ext in (".py", ".md", ".rst", ".js", ".ts", ".sh", ".ps1"):
        return False      # source/docs that merely mention secrets or keys (e.g. secrets_helper.py)
    # data/config files named like secrets or API keys: secrets.yaml, api_keys.json, my-key.txt
    return "secret" in name or bool(
        re.search(r"(^|[._-])(api|private|access|auth|ssh|signing)[._-]?keys?($|[._-])|^keys?$", base))


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
    (r"reset_drawdown_halt\s*\(", "clearing the drawdown halt must be a human decision"),
    (r">>?\s*[\"']?\S*risk_state\.json", "the drawdown-halt state file must not be redirected over"),
    (r"\b(rm|mv|cp|truncate|tee|unlink|shred|Remove-Item|del|ln|dd)\b[^;&|\n]*risk_state\.json",
     "the drawdown-halt state file must not be removed or rewritten by scripts"),
    (r"\bsed\s+-i[^;&|\n]*risk_state\.json", "the drawdown-halt state file must not be edited in place"),
    (r"(open\(|write_text|write_bytes|json\.dump|os\.replace|shutil\.)[^\n]*risk_state|"
     r"risk_state[^\n]*(write_text|write_bytes|json\.dump|os\.replace|open\([^)]*[\"'][wa+])",
     "the drawdown-halt state file must not be written by scripts"),
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
# Commands whose quoted text is a message, not something executed (checked per command segment).
MESSAGE_CMDS = re.compile(r"^\s*(git\s+commit|gh\s+(pr|issue)\b|echo\b|printf\b)")
READERS = {"cat", "head", "tail", "less", "more", "bat", "nl", "strings", "xxd", "od", "cp", "mv",
           "scp", "base64", "tee", "grep", "rg", "egrep", "fgrep", "awk", "sed", "type", "get-content"}
LIVE_IN_CONTENT = re.compile(LIVE, re.I)
DOC_EXT = (".md", ".rst", ".txt")


HEREDOC_SUBST = re.compile(r"\$\(\s*cat\s*<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*\)", re.S)


def split_segments(cmd):
    """Split on unquoted && || ; and newlines, so a quoted message cannot hide a second command."""
    segs, cur, quote, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            cur.append(c)
            if c == quote and (quote == "'" or cmd[i - 1] != "\\"):
                quote = None
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            segs.append("".join(cur))
            cur = []
            i += 1
        elif c in ";\n":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    return [x for x in segs if x.strip()]


def scrub_quotes(seg):
    """Blank quoted message text, but never text the shell would execute: single quotes are
    literal; double quotes are kept when they contain $(...), `...` or ${...}; a $(cat <<EOF ...)
    heredoc is just text, so its body is dropped."""
    seg = HEREDOC_SUBST.sub('""', seg)

    def repl(m):
        txt = m.group(0)
        if txt.startswith('"') and re.search(r"\$\(|`|\$\{", txt):
            return txt
        return '""'
    return re.sub(r"\"[^\"]*\"|'[^']*'", repl, seg)


def prepare_scan(cmd):
    """The text to pattern-match: message segments scrubbed, everything else raw."""
    out = []
    for seg in split_segments(cmd):
        out.append(scrub_quotes(seg) if MESSAGE_CMDS.match(seg) else seg)
    return " ; ".join(out)


def bash_reads_secret_file(cmd):
    import shlex
    for seg in split_segments(cmd):
        try:
            words = shlex.split(seg, posix=True)
        except ValueError:
            words = seg.split()
        if not words or os.path.basename(words[0]).lower() not in READERS:
            continue
        if any(not w.startswith("-") and is_secret_path(w) for w in words[1:]):
            return True
    return False


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
        if tool != "Read" and re.match(r"risk_state\.json", os.path.basename(str(path or ""))):
            block("the drawdown-halt state file must only be changed by the running agent or "
                  "scripts/reset_drawdown_halt.py (typed by a human)")
        if tool != "Read" and not str(path or "").lower().endswith(DOC_EXT):
            if LIVE_IN_CONTENT.search(edit_text(ti)):
                block("do not hard-code TRADING_MODE=live in source")
    elif tool == "Grep":
        if is_secret_path(ti.get("path")) or re.search(r"\.env$", str(ti.get("glob") or "")):
            block(".env cannot be searched")
    elif tool == "Bash":
        cmd = str(ti.get("command") or "")
        scan = prepare_scan(cmd)
        for pat, why in BASH_BLOCKED:
            if re.search(pat, scan, re.I):
                block(why)
        for pat in READS_ENV:
            if re.search(pat, scan, re.I):
                block("this command would read or dump .env / environment secrets")
        if bash_reads_secret_file(scan):
            block("this command would read a secrets / API-key file")


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
