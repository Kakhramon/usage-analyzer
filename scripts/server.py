#!/usr/bin/env python3
"""Usage dashboard: one URL, one sqlite file, no dependencies.

    python3 server.py --port 8080 --token secret
    open http://host:8080/
"""
import argparse, json, os, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
DB = os.environ.get("USAGE_DB", str(HERE / "usage.db"))
TOKEN = os.environ.get("USAGE_TOKEN", "")
MAX_BODY = 8 << 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT NOT NULL,
  user TEXT NOT NULL,
  tool TEXT NOT NULL,
  project TEXT, models TEXT,
  prompts INTEGER, assistant_turns INTEGER, tool_calls INTEGER,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  started_at TEXT, ended_at TEXT,
  PRIMARY KEY (user, session_id)
);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started_at);
"""

FIELDS = ["session_id", "tool", "project", "models", "prompts", "assistant_turns", "tool_calls",
          "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
          "reasoning_tokens", "started_at", "ended_at"]

STATS_SQL = """
SELECT user, tool,
       COUNT(*) sessions, SUM(prompts) prompts, SUM(tool_calls) tool_calls,
       SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
       SUM(cache_read_tokens) cache_read_tokens, SUM(cache_write_tokens) cache_write_tokens,
       SUM(reasoning_tokens) reasoning_tokens,
       SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) total_tokens,
       SUM(COALESCE(julianday(ended_at) - julianday(started_at), 0)) * 1440 active_minutes,
       MAX(ended_at) last_seen
FROM sessions WHERE started_at >= ? GROUP BY user, tool ORDER BY total_tokens DESC
"""


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def ingest(payload):
    user = str(payload.get("user", "")).strip()[:64]
    sessions = payload.get("sessions")
    if not user or not isinstance(sessions, list):
        raise ValueError("need user and sessions[]")
    rows = []
    for s in sessions[:500]:
        if not isinstance(s, dict) or not s.get("session_id"):
            raise ValueError("session needs session_id")
        r = [user]
        for f in FIELDS:
            v = s.get(f)
            if f == "models":
                v = ",".join(v) if isinstance(v, list) else (v or "")
            elif f in ("session_id", "tool", "project", "started_at", "ended_at"):
                v = str(v)[:512] if v is not None else None
            else:
                v = int(v or 0)
            r.append(v)
        rows.append(r)
    cols = ", ".join(["user"] + FIELDS)
    marks = ", ".join("?" * (len(FIELDS) + 1))
    with db() as c:
        c.executemany(f"INSERT OR REPLACE INTO sessions ({cols}) VALUES ({marks})", rows)
    return {"ok": True, "stored": len(rows)}


def stats(since):
    with db() as c:
        return {"by_user": [dict(r) for r in c.execute(STATS_SQL, (since,))]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/stats":
            since = urllib.parse.parse_qs(u.query).get("since", ["0000"])[0][:32]
            return self._send(200, stats(since))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/ingest":
            return self._send(404, {"error": "not found"})
        if TOKEN and self.headers.get("X-Token", "") != TOKEN:
            return self._send(401, {"error": "bad token"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": "bad body size"})
        try:
            self._send(200, ingest(json.loads(self.rfile.read(n))))
        except (ValueError, TypeError, sqlite3.Error) as e:
            self._send(400, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--token", default=TOKEN)
    a = ap.parse_args()
    TOKEN = a.token
    if not TOKEN:
        print("WARNING: no --token, anyone can post usage data")
    print(f"dashboard: http://{a.host}:{a.port}/   db: {DB}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
