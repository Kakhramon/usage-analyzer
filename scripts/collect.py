#!/usr/bin/env python3
"""Scan local Claude Code + Codex session logs, push per-session stats to a dashboard URL.

Config: ~/.usage-analyzer.json  {"name": "...", "url": "...", "token": "..."}
Usage:
    python3 collect.py init --name alice --url https://host/ [--token secret]
    python3 collect.py            # scan + push (run from cron / a hook)
    python3 collect.py --dry-run  # print what would be sent
    python3 collect.py --quiet    # hook mode: no output, never fails
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path.home() / ".usage-analyzer.json"
STATE = Path.home() / ".usage-analyzer-state.json"
LOCK = Path.home() / ".usage-analyzer.lock"
HELD_LOCK = False
STALE_LOCK_SECONDS = 600
CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
MAX_PROMPTS_PER_SESSION = 500

# Credentials must never leave the machine, whatever the server asks for.
SECRETS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|xox[abprs]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\b(?:bearer|api[_\-]?key|token|password|secret)\b\s*[:=]\s*\S{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
NOISE = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+)>[\s\S]*?</\1>")


def clean_prompt(text):
    text = NOISE.sub("", text or "").strip()
    for rx in SECRETS:
        text = rx.sub("[redacted]", text)
    return text


def add_prompt(s, text, at):
    s["prompts"] += 1
    text = clean_prompt(text)
    if text and len(s["prompt_texts"]) < MAX_PROMPTS_PER_SESSION:
        s["prompt_texts"].append({"at": at, "chars": len(text), "text": text})


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in ("text", "input_text"))
    return ""


def _ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blank(sid, tool, path):
    return {
        "session_id": sid, "tool": tool, "source_file": str(path), "project": "",
        "models": set(), "prompts": 0, "assistant_turns": 0, "tool_calls": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "reasoning_tokens": 0, "started_at": None, "ended_at": None,
        "prompt_texts": [],
    }


def _bump_time(s, t):
    if t is None:
        return
    s["started_at"] = t if s["started_at"] is None else min(s["started_at"], t)
    s["ended_at"] = t if s["ended_at"] is None else max(s["ended_at"], t)


def parse_claude(path):
    s = _blank(path.stem, "claude", path)
    for line in path.open(errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("isSidechain"):
            continue  # subagent traffic belongs to its parent session
        _bump_time(s, _ts(d.get("timestamp")))
        s["project"] = s["project"] or d.get("cwd", "")
        msg = d.get("message") or {}
        if d.get("type") == "user":
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            if not any(b.get("type") == "tool_result" for b in blocks if isinstance(b, dict)):
                add_prompt(s, text_of(content), d.get("timestamp"))
        elif d.get("type") == "assistant":
            s["assistant_turns"] += 1
            if msg.get("model"):
                s["models"].add(msg["model"])
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    s["tool_calls"] += 1
            u = msg.get("usage") or {}
            s["input_tokens"] += u.get("input_tokens", 0)
            s["output_tokens"] += u.get("output_tokens", 0)
            s["cache_read_tokens"] += u.get("cache_read_input_tokens", 0)
            s["cache_write_tokens"] += u.get("cache_creation_input_tokens", 0)
            s["reasoning_tokens"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
    return s


def parse_codex(path):
    s = _blank(path.stem, "codex", path)
    for line in path.open(errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        p = d.get("payload") or {}
        kind = d.get("type")
        _bump_time(s, _ts(d.get("timestamp")))
        if kind == "session_meta":
            s["session_id"] = p.get("session_id") or s["session_id"]
            s["project"] = p.get("cwd", "")
        elif kind == "turn_context":
            if p.get("model"):
                s["models"].add(p["model"])
        elif kind == "event_msg":
            if p.get("type") == "token_count":
                # total_token_usage is cumulative for the session; keep the largest seen
                t = (p.get("info") or {}).get("total_token_usage") or {}
                s["input_tokens"] = max(s["input_tokens"], t.get("input_tokens", 0))
                s["output_tokens"] = max(s["output_tokens"], t.get("output_tokens", 0))
                s["cache_read_tokens"] = max(s["cache_read_tokens"], t.get("cached_input_tokens", 0))
                s["cache_write_tokens"] = max(s["cache_write_tokens"], t.get("cache_write_input_tokens", 0))
                s["reasoning_tokens"] = max(s["reasoning_tokens"], t.get("reasoning_output_tokens", 0))
            elif p.get("type") == "task_started":
                s["assistant_turns"] += 1
        elif kind == "response_item":
            if p.get("type") == "message" and p.get("role") == "user":
                add_prompt(s, text_of(p.get("content")), d.get("timestamp"))
            elif p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                s["tool_calls"] += 1
    # codex counts input_tokens inclusive of cached ones
    s["input_tokens"] = max(0, s["input_tokens"] - s["cache_read_tokens"])
    return s


def scan(state, capture="off", max_chars=500):
    """Yield sessions for log files whose (size, mtime, capture mode) changed."""
    files = []
    if CLAUDE_DIR.is_dir():
        files += [(f, parse_claude) for f in CLAUDE_DIR.rglob("*.jsonl")]
    if CODEX_DIR.is_dir():
        files += [(f, parse_codex) for f in CODEX_DIR.rglob("*.jsonl")]
    for path, parser in files:
        try:
            st = path.stat()
        except OSError:
            continue
        fp = f"{st.st_size}:{int(st.st_mtime)}:{capture}:{max_chars}"
        if state.get(str(path)) == fp:
            continue
        try:
            s = parser(path)
        except Exception as e:  # one corrupt log must not kill the run
            print(f"skip {path}: {e}", file=sys.stderr)
            continue
        if not s["prompts"] and not s["output_tokens"]:
            state[str(path)] = fp
            continue
        s["models"] = sorted(s["models"])
        if capture == "off":
            s["prompt_texts"] = []
        elif capture == "truncated":
            for t in s["prompt_texts"]:
                t["text"] = t["text"][:max_chars]
        for k in ("started_at", "ended_at"):
            s[k] = datetime.fromtimestamp(s[k], timezone.utc).isoformat() if s[k] else None
        yield str(path), fp, s


def fetch_settings(cfg):
    """What the server wants collected. Unreachable server means collect nothing extra."""
    req = urllib.request.Request(cfg["url"].rstrip("/") + "/api/config",
                                 headers={"X-Token": cfg.get("token", "")})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            s = json.load(r)
        return str(s.get("capture_prompts", "off")), int(s.get("prompt_max_chars", 500))
    except (urllib.error.URLError, ValueError, KeyError, TimeoutError, OSError):
        return "off", 500


def push(cfg, sessions):
    body = json.dumps({"user": cfg["name"], "sessions": sessions}).encode()
    req = urllib.request.Request(
        cfg["url"].rstrip("/") + "/api/ingest", data=body,
        headers={"Content-Type": "application/json", "X-Token": cfg.get("token", "")},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def acquire_lock():
    """Single-flight: two sessions ending at once must not both rewrite STATE."""
    # ponytail: lock file, good enough for one machine; needs real locking only if
    # collect ever runs on shared storage.
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime > STALE_LOCK_SECONDS:
            LOCK.unlink()
        os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except (FileExistsError, OSError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="push", choices=["init", "push"])
    ap.add_argument("--name")
    ap.add_argument("--url")
    ap.add_argument("--token", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="hook mode: silent, exit 0")
    a = ap.parse_args()

    if a.cmd == "init":
        if not a.name or not a.url:
            sys.exit("init needs --name and --url")
        CONFIG.write_text(json.dumps({"name": a.name, "url": a.url, "token": a.token}, indent=2))
        CONFIG.chmod(0o600)
        print(f"wrote {CONFIG}")
        return

    if not CONFIG.exists():
        if a.quiet:
            return
        sys.exit("not configured: python3 collect.py init --name you --url https://host/")
    if not a.dry_run:
        if not acquire_lock():
            return  # another run is already syncing
        global HELD_LOCK
        HELD_LOCK = True
    cfg = json.loads(CONFIG.read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    capture, max_chars = fetch_settings(cfg)
    batch, marks = [], []
    for path, fp, s in scan(state, capture, max_chars):
        batch.append(s)
        marks.append((path, fp))
        if len(batch) >= 50 and not a.dry_run:
            push(cfg, batch)
            for p, f in marks:
                state[p] = f
            batch, marks = [], []
    if batch:
        if a.dry_run:
            print(json.dumps(batch, indent=2)[:4000])
            return
        push(cfg, batch)
        for p, f in marks:
            state[p] = f
    STATE.write_text(json.dumps(state))
    if not a.quiet:
        print(f"synced ({capture} prompt capture), {len(state)} log files tracked")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a hook must never break the session it runs in
        if "--quiet" not in sys.argv:
            raise
        print(f"usage-analyzer: {e}", file=sys.stderr)
    finally:
        if HELD_LOCK:
            LOCK.unlink(missing_ok=True)
