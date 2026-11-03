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

Commands are split into segments on unquoted && || | |& & ; and newlines (honoring quotes and
escapes) and every segment is checked. Message text (commit messages, PR bodies, echo) is ignored
only when the shell would not execute it. This is still pattern matching and can be bypassed by
runtime string building; for real enforcement also use Claude Code permission deny rules.
"""
import fnmatch
import json
import os
import re
import sys

BLOCK = 2


def block(msg):
    print(f"BLOCKED by .claude/hooks/guard.py: {msg}", file=sys.stderr)
    sys.exit(BLOCK)


SECRET_NAMES = {"credentials.json", "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                ".netrc", ".pgpass", ".npmrc", ".pypirc"}
# Canonical secret file names, used to judge globs/braces like `cat .en?` or `api_key{s,}.json`.
SECRET_SAMPLES = [".env", ".env.local", ".env.production", "api_keys.json", "api_key.json",
                  "secrets.json", "secrets.yaml", "secret.txt", "keys.txt", "credentials",
                  "credentials.json", "id_rsa", "id_ed25519", ".netrc", ".pgpass", "server.pem",
                  "private.key"]


def is_secret_path(path):
    name = os.path.basename(str(path or "")).lower()
    if not name or name == ".env.example":
        return False
    base, ext = os.path.splitext(name)
    if name == ".env" or name.startswith(".env.") or ext in (".pem", ".key"):
        return True
    if name in SECRET_NAMES:
        return True
    if ext in (".py", ".md", ".rst", ".js", ".ts", ".sh", ".ps1"):
        return False      # source/docs that merely mention secrets or keys (e.g. secrets_helper.py)
    # data/config files named like secrets or API keys: secrets.yaml, api_keys.json, my-key.txt
    return "secret" in name or bool(
        re.search(r"(^|[._-])(api|private|access|auth|ssh|signing)[._-]?keys?($|[._-])|^keys?$", base))


def _brace_expand(word):
    m = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not m:
        return [word]
    out = []
    for alt in m.group(1).split(","):
        out += _brace_expand(word[:m.start()] + alt + word[m.end():])
    return out[:64]


def looks_secret(word):
    """Literal secret path, or a glob/brace pattern that could expand to one."""
    if is_secret_path(word):
        return True
    if not re.search(r"[*?\[{]", word):
        return False
    for w in _brace_expand(word):
        if is_secret_path(w):
            return True
        pat = os.path.basename(w).lower()
        if any(fnmatch.fnmatchcase(n, pat) for n in SECRET_SAMPLES):
            return True
    return False


# .env, .env.local, .env.production ... but not .env.example
ENVTOK = r"\.env(?!\.example(?![\w]))(?:\.[\w-]+)*(?![\w])"
LIVE = r"TRADING_MODE[\"'\]\s]*[=:][\"'\s]*live"

BASH_BLOCKED = [   # matched per command segment
    (LIVE, "TRADING_MODE=live must be set by a human, not scripted"),
    (r"--i-understand-the-risk", "live-trading acknowledgement must be typed by a human"),
    (r"git\s+push\b.*\s(--force(?![-\w])|-f(?![-\w]))", "force-push is not allowed"),
    (r"git\s+push\b.*\s\+[\w/.-]+", "force-push via +refspec is not allowed"),
    (r"rm\s+-rf?\s+/(\s|$)", "destructive rm"),
    (r"DROP\s+TABLE", "destructive SQL"),
    (r"DELETE\s+FROM.*WHERE\s+1\s*=\s*1", "destructive SQL"),
    (r"liquidate_all", "liquidating positions must be a human decision"),
    (r"reset_drawdown_halt\s*\(", "clearing the drawdown halt must be a human decision"),
    (r">>?\s*[\"']?\S*risk_state\.json", "the drawdown-halt state file must not be redirected over"),
    (r"\b(rm|unlink|shred|truncate|tee|Remove-Item|del|dd)\b.*risk_state",
     "the drawdown-halt state file must not be removed or rewritten by scripts"),
    (r"\bsed\s+(-\w*\s+)*-i.*risk_state", "the drawdown-halt state file must not be edited in place"),
    (r"\bgit\s+clean\b.*\s-\w*[xX]", "git clean -x would delete ignored files such as logs/risk_state.json"),
]
READS_ENV = [      # matched per command segment
    rf"\b(cat|type|head|tail|less|more|bat|nl|strings|xxd|od|cp|mv|scp|base64|tee|jq)\s+.*{ENVTOK}",
    rf"Get-Content\s+.*{ENVTOK}",
    rf"<\s*[\"']?[^\s]*{ENVTOK}",
    rf"\b(source|\.)\s+[\"']?[^\s]*{ENVTOK}",
    rf"open\([^)]*{ENVTOK}",
    r"^\s*(printenv|env)\s*$",
]
STATE_WRITE_VERBS = re.compile(
    r"open\([^)]*['\"][wax+]|write_text|write_bytes|json\.dump|os\.(replace|remove|unlink|rename)|"
    r"\.unlink\(|\.rename\(|\.replace\(|shutil\.|truncate\(")
READ_ONLY_TOOLS = {"grep", "rg", "egrep", "fgrep", "cat", "less", "more", "head", "tail", "wc", "git",
                   "diff", "ls", "find_no_delete"}
# Commands whose quoted text is a message, not something executed (checked per command segment).
MESSAGE_CMDS = re.compile(r"^\s*(git\s+commit|gh\s+(pr|issue)\b|echo\b|printf\b)")
READERS = {"cat", "head", "tail", "less", "more", "bat", "nl", "strings", "xxd", "od", "hexdump",
           "cp", "mv", "scp", "rsync", "base64", "tee", "grep", "rg", "egrep", "fgrep", "awk", "sed",
           "type", "get-content", "jq", "yq", "cut", "sort", "uniq", "diff", "vim", "vi", "nano",
           "emacs", "view", "openssl", "gpg", "tar", "zip"}
PATTERN_FIRST = {"grep", "rg", "egrep", "fgrep", "awk", "sed", "jq", "yq"}
WRAPPERS = {"sudo", "doas", "command", "nice", "nohup", "time", "stdbuf", "exec", "builtin"}
LIVE_IN_CONTENT = re.compile(LIVE, re.I)
DOC_EXT = (".md", ".rst", ".txt")

SEPARATORS = ("&&", "||", "|&", ";", "\n", "|", "&")


def split_segments(cmd):
    """Split a shell command on unquoted && || | |& & ; and newlines.

    Honors backslash escapes outside quotes and inside double quotes, and single quotes as literal.
    Returns (segments, has_pipe)."""
    segs, cur, quote, i, has_pipe = [], [], None, 0, False
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if quote == "'":
            cur.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:          # escape (outside quotes or inside double quotes)
            if quote is None and cmd[i + 1] == "\n":
                i += 2                        # line continuation joins lines
                continue
            cur.append(cmd[i:i + 2])
            i += 2
            continue
        if quote == '"':
            cur.append(c)
            if c == '"':
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            cur.append(c)
            i += 1
            continue
        sep = next((x for x in SEPARATORS if cmd.startswith(x, i)), None)
        if sep == "&" and ((i > 0 and cmd[i - 1] in "<>") or cmd.startswith("&>", i)):
            sep = None                        # 2>&1, &> redirects are not separators
        if sep:
            if sep in ("|", "|&"):
                has_pipe = True
            segs.append("".join(cur))
            cur = []
            i += len(sep)
            continue
        cur.append(c)
        i += 1
    segs.append("".join(cur))
    return [x for x in segs if x.strip()], has_pipe


SAFE_HEREDOC = re.compile(r"\$\(\s*cat\s*<<-?\s*(['\"])(\w+)\1[ \t]*\n")


def strip_safe_heredocs(seg):
    """Replace $(cat <<'DELIM' ... DELIM) with "" ONLY when bash would treat the body as plain text:
    the delimiter is quoted (no expansion inside), the body ends at the FIRST line equal to the
    delimiter, and only whitespace follows before the closing parenthesis. Otherwise leave it."""
    out, pos = [], 0
    for m in SAFE_HEREDOC.finditer(seg):
        if m.start() < pos:
            continue
        delim, body_start = m.group(2), m.end()
        rest = seg[body_start:]
        lines = rest.split("\n")
        end_idx, offset = None, 0
        for ln in lines:
            if ln.strip() == delim:
                end_idx = offset + len(ln)
                break
            offset += len(ln) + 1
        if end_idx is None:
            continue
        after = rest[end_idx:]
        close = re.match(r"\s*\)", after)
        if not close:
            continue                          # something other than whitespace before ")" runs
        out.append(seg[pos:m.start()])
        out.append('""')
        pos = body_start + end_idx + close.end()
    out.append(seg[pos:])
    return "".join(out)


def scrub_message(seg):
    """Blank the quoted text of a message command (commit/PR text, echo), but never anything the
    shell would execute. Returns the raw segment if it contains $(...) or backticks."""
    seg = strip_safe_heredocs(seg)
    if "$(" in seg or "`" in seg:
        return seg
    return re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", '""', seg)


MSG_HEREDOC_STDIN = re.compile(
    r"^\s*(git\s+commit|gh\s+(pr|issue)\b)[^\n]*<<-?\s*(['\"])(\w+)\3[ \t]*$")


def strip_message_stdin_heredocs(cmd):
    """`git commit -F - <<'EOF'` bodies are message text (quoted delimiter: no expansion)."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        m = MSG_HEREDOC_STDIN.match(lines[i])
        if m:
            delim = m.group(4)
            j = next((k for k in range(i + 1, len(lines)) if lines[k].strip() == delim), None)
            if j is not None:
                out.append(lines[j])
                i = j + 1
                continue
        i += 1
    return "\n".join(out)


def prepare_segments(cmd):
    """Segments to pattern-match: message segments scrubbed (unless anything is piped), others raw."""
    segs, has_pipe = split_segments(strip_message_stdin_heredocs(cmd))
    if has_pipe:
        return segs                           # `echo '...' | bash`: the message IS the program
    return [scrub_message(x) if MESSAGE_CMDS.match(x) else x for x in segs]


WRAPPER_OPTS_WITH_ARG = {"-n", "-u", "-g", "-C", "-D", "-p", "-T", "-h", "-R", "-r", "-t", "-i", "-o",
                         "-e", "-c", "-U", "-S", "-a", "--user", "--group", "--adjustment"}


def words_of(seg):
    import shlex
    try:
        words = shlex.split(seg, posix=True)
    except ValueError:
        words = seg.split()
    while words and (os.path.basename(words[0]) in WRAPPERS or re.match(r"^\w+=", words[0])
                     or (os.path.basename(words[0]) == "env" and len(words) > 1)):
        words = words[1:]
        while words and words[0].startswith("-") and words[0] != "--":
            takes_arg = words[0] in WRAPPER_OPTS_WITH_ARG
            words = words[1:]
            if takes_arg and words:
                words = words[1:]             # e.g. `nice -n 5 cat x`, `sudo -u bob cat x`
        if words and words[0] == "--":
            words = words[1:]
    return words


def segment_reads_secret(seg):
    words = words_of(seg)
    if not words:
        return False
    tool = os.path.basename(words[0]).lower()
    if tool in READERS:
        args = [w for w in words[1:]]
        if tool in PATTERN_FIRST and not any(w in ("-e", "-f", "--regexp", "--file") for w in args):
            non_opt = [k for k, w in enumerate(args) if not w.startswith("-")]
            if non_opt:
                args.pop(non_opt[0])          # the search pattern / program, not a path
        if any(not w.startswith("-") and looks_secret(w) for w in args):
            return True
    for m in re.finditer(r"(?:open|Path)\(\s*['\"]([^'\"]+)['\"]", seg):
        if looks_secret(m.group(1)):
            return True
    return False


def segment_touches_state(seg):
    """Deleting/moving/overwriting logs/risk_state.json (the persisted drawdown halt)."""
    words = words_of(seg)
    tool = os.path.basename(words[0]).lower() if words else ""
    args = [w for w in words[1:] if not w.startswith("-")]
    flags = "".join(w.lstrip("-") for w in words[1:] if w.startswith("-") and not w.startswith("--"))
    longflags = [w for w in words[1:] if w.startswith("--")]
    norm = lambda a: a.rstrip("/").replace("./", "")  # noqa: E731
    logs_dir = lambda a: norm(a) in ("logs", ".") or norm(a).endswith("/logs")  # noqa: E731
    logs_only = lambda a: norm(a) == "logs" or norm(a).endswith("/logs")  # noqa: E731
    hits_state = lambda a: "risk_state" in a or fnmatch.fnmatchcase("logs/risk_state.json", norm(a))  # noqa: E731
    if tool in ("rm", "unlink", "shred", "truncate", "del", "remove-item"):
        if any(hits_state(a) for a in args):
            return True
        recursive = "r" in flags.lower() or "--recursive" in longflags
        if recursive and any(logs_dir(a) for a in args):
            return True
    if tool == "mv" and any(hits_state(a) for a in args):
        return True
    if tool in ("cp", "install", "ln", "rsync") and args and hits_state(args[-1]):
        return True                           # cp risk_state.json /tmp/backup is fine (source)
    if tool == "find" and ("-delete" in words or "-exec" in words) and (
            "risk_state" in seg or any(logs_only(a) for a in args[:1])):
        if "-delete" in words or re.search(r"-exec\s+(rm|shred|unlink|truncate|mv)\b", seg):
            return True
    if "risk_state" in seg and tool not in READ_ONLY_TOOLS and STATE_WRITE_VERBS.search(seg):
        return True                           # python -c "...os.remove('logs/risk_state.json')"
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
        for seg in prepare_segments(cmd):
            for pat, why in BASH_BLOCKED:
                if re.search(pat, seg, re.I | re.S):
                    block(why)
            for pat in READS_ENV:
                if re.search(pat, seg, re.I | re.S):
                    block("this command would read or dump .env / environment secrets")
            if segment_reads_secret(seg):
                block("this command would read a secrets / API-key file")
            if segment_touches_state(seg):
                block("the drawdown-halt state file must only be changed by the running agent or "
                      "scripts/reset_drawdown_halt.py (typed by a human)")


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
