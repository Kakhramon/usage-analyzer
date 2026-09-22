#!/usr/bin/env python3
"""Usage dashboard: one URL, one sqlite file, no dependencies.

    python3 server.py --port 8080 --token secret [--admin-token other]
    open http://host:8080/

Everything the collectors do is configured here, after deployment: the Settings
panel on the dashboard writes to the settings table, and every collector reads it
on its next sync.
"""
import argparse, json, os, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
DB = os.environ.get("USAGE_DB", str(HERE / "usage.db"))
TOKEN = os.environ.get("USAGE_TOKEN", "")
ADMIN_TOKEN = os.environ.get("USAGE_ADMIN_TOKEN", "")
MAX_BODY = 32 << 20

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
CREATE TABLE IF NOT EXISTS prompts (
  user TEXT NOT NULL, session_id TEXT NOT NULL, idx INTEGER NOT NULL,
  at TEXT, chars INTEGER, text TEXT,
  PRIMARY KEY (user, session_id, idx)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  user TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, last_sync TEXT, machine TEXT
);
"""

# Anything a collector or the dashboard may change after deployment.
DEFAULTS = {
    "retention_days": "0",         # 0 keeps everything
    "team_name": "Usage Analyzer",
}
INT_KEYS = {"retention_days"}

FIELDS = ["session_id", "tool", "project", "models", "prompts", "assistant_turns", "tool_calls",
          "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
          "reasoning_tokens", "started_at", "ended_at"]

AGG = """SUM(prompts) prompts, SUM(tool_calls) tool_calls,
       SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
       SUM(cache_read_tokens) cache_read_tokens, SUM(cache_write_tokens) cache_write_tokens,
       SUM(reasoning_tokens) reasoning_tokens,
       SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) total_tokens,
       SUM(COALESCE(julianday(ended_at) - julianday(started_at), 0)) * 1440 active_minutes,
       MAX(ended_at) last_seen"""

WINDOW = "started_at >= :since AND started_at < :until"
STATS_SQL = f"SELECT user, tool, COUNT(*) sessions, {AGG} FROM sessions " \
            f"WHERE {WINDOW} AND (:user = '' OR user = :user) " \
            "GROUP BY user, tool ORDER BY total_tokens DESC"
PROJECTS_SQL = f"SELECT project, COUNT(*) sessions, {AGG} FROM sessions " \
               f"WHERE user = :user AND {WINDOW} " \
               "GROUP BY project ORDER BY total_tokens DESC LIMIT 50"
SESSIONS_SQL = """
SELECT session_id, tool, project, models, prompts, assistant_turns, tool_calls,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
       input_tokens + output_tokens + cache_read_tokens + cache_write_tokens total_tokens,
       started_at, ended_at,
       (SELECT COUNT(*) FROM prompts p WHERE p.user = s.user AND p.session_id = s.session_id) saved_prompts
FROM sessions s WHERE user = :user AND started_at >= :since AND started_at < :until
ORDER BY started_at DESC LIMIT 200
"""


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def get_settings(conn=None):
    c = conn or db()
    try:
        s = dict(DEFAULTS)
        s.update({r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")})
        for k in INT_KEYS:
            s[k] = int(s[k])
        return s
    finally:
        if conn is None:
            c.close()


def set_settings(payload):
    if not isinstance(payload, dict):
        raise ValueError("settings must be an object")
    clean = {}
    for k, v in payload.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown setting: {k}")
        if k in INT_KEYS:
            v = int(v)
            if v < 0 or v > 1_000_000:
                raise ValueError(f"{k} out of range")
        else:
            v = str(v)[:128]
        clean[k] = str(v)
    with db() as c:
        c.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                      list(clean.items()))
        return get_settings(c)


def purge(conn):
    days = get_settings(conn)["retention_days"]
    if days <= 0:
        return
    cutoff = f"date('now', '-{int(days)} days')"
    conn.execute(f"DELETE FROM prompts WHERE (user, session_id) IN "
                 f"(SELECT user, session_id FROM sessions WHERE date(started_at) < {cutoff})")
    conn.execute(f"DELETE FROM sessions WHERE date(started_at) < {cutoff}")


def ingest(payload):
    user = str(payload.get("user", "")).strip()[:64]
    sessions = payload.get("sessions")
    if not user or not isinstance(sessions, list):
        raise ValueError("need user and sessions[]")
    rows, prompt_rows = [], []
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
        for i, p in enumerate((s.get("prompt_texts") or [])[:500]):
            if not isinstance(p, dict):
                raise ValueError("prompt_texts entries must be objects")
            prompt_rows.append([user, str(s["session_id"])[:512], i,
                                str(p.get("at") or "")[:64], int(p.get("chars") or 0),
                                str(p.get("text") or "")[:100_000]])
    machine = str(payload.get("machine") or "")[:128]
    seen = [s.get("started_at") for s in sessions if s.get("started_at")]
    first, last = (min(seen), max(seen)) if seen else (None, None)
    cols = ", ".join(["user"] + FIELDS)
    marks = ", ".join("?" * (len(FIELDS) + 1))
    with db() as c:
        c.executemany(f"INSERT OR REPLACE INTO sessions ({cols}) VALUES ({marks})", rows)
        c.execute("""INSERT INTO users (user, first_seen, last_seen, last_sync, machine)
                     VALUES (?, ?, ?, datetime('now'), ?)
                     ON CONFLICT(user) DO UPDATE SET
                       first_seen = MIN(COALESCE(first_seen, excluded.first_seen), excluded.first_seen),
                       last_seen  = MAX(COALESCE(last_seen,  excluded.last_seen),  excluded.last_seen),
                       last_sync  = excluded.last_sync,
                       machine    = COALESCE(NULLIF(excluded.machine, ''), machine)""",
                  (user, first, last, machine))
        if prompt_rows:
            c.executemany("INSERT OR REPLACE INTO prompts (user, session_id, idx, at, chars, text) "
                          "VALUES (?, ?, ?, ?, ?, ?)", prompt_rows)
        purge(c)
    return {"ok": True, "stored": len(rows), "prompts_stored": len(prompt_rows)}


def window(since="", until="", user=""):
    """Half-open [since, until). Empty ends mean unbounded."""
    return {"since": since or "0000", "until": (until or "9999") + "~", "user": user or ""}


def stats(since="", until="", user=""):
    w = window(since, until, user)
    with db() as c:
        return {"by_user": [dict(r) for r in c.execute(STATS_SQL, w)],
                "users": [dict(r) for r in c.execute(
                    "SELECT user, first_seen, last_seen, last_sync, machine FROM users ORDER BY user")],
                "settings": get_settings(c)}


def user_detail(user, since="", until=""):
    w = window(since, until, user)
    with db() as c:
        return {"user": user,
                "projects": [dict(r) for r in c.execute(PROJECTS_SQL, w)],
                "sessions": [dict(r) for r in c.execute(SESSIONS_SQL, w)]}


def session_prompts(user, session_id):
    with db() as c:
        return {"prompts": [dict(r) for r in c.execute(
            "SELECT idx, at, chars, text FROM prompts WHERE user = ? AND session_id = ? ORDER BY idx",
            (user, session_id))]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "usage-analyzer"

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

    def _q(self, u, key, default=""):
        return urllib.parse.parse_qs(u.query).get(key, [default])[0][:512]

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            raise ValueError("bad body size")
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "dashboard.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if u.path == "/api/stats":
                return self._send(200, stats(self._q(u, "since"), self._q(u, "until"),
                                             self._q(u, "user")))
            if u.path == "/api/user":
                user = self._q(u, "user")
                if not user:
                    return self._send(400, {"error": "user required"})
                return self._send(200, user_detail(user, self._q(u, "since"), self._q(u, "until")))
            if u.path == "/api/prompts":
                return self._send(200, session_prompts(self._q(u, "user"), self._q(u, "session")))
            if u.path == "/api/config":
                if TOKEN and self.headers.get("X-Token", "") != TOKEN:
                    return self._send(401, {"error": "bad token"})
                return self._send(200, get_settings())
        except (ValueError, sqlite3.Error) as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/ingest":
                if TOKEN and self.headers.get("X-Token", "") != TOKEN:
                    return self._send(401, {"error": "bad token"})
                return self._send(200, ingest(self._body()))
            if path == "/api/settings":
                if self.headers.get("X-Admin-Token", "") != (ADMIN_TOKEN or TOKEN):
                    return self._send(401, {"error": "bad admin token"})
                return self._send(200, set_settings(self._body()))
        except (ValueError, TypeError, sqlite3.Error) as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--token", default=TOKEN, help="shared secret every collector sends")
    ap.add_argument("--admin-token", default=ADMIN_TOKEN,
                    help="secret required to change settings (defaults to --token)")
    ap.add_argument("--demo", action="store_true", help="seed sample data if the db is empty")
    a = ap.parse_args()
    TOKEN, ADMIN_TOKEN = a.token, a.admin_token
    if not TOKEN:
        print("WARNING: no --token, anyone can post usage data")
    if a.demo:
        import seed_demo
        seed_demo.seed()
    print(f"dashboard: http://{a.host}:{a.port}/   db: {DB}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
