#!/usr/bin/env python3
"""Fill the database with sample data so the dashboard has something to show.

    python3 seed_demo.py            # seed (refuses if real data is present)
    python3 seed_demo.py --force    # seed anyway
    python3 seed_demo.py --clear    # remove the sample users again

Sample users are named demo-*, so --clear never touches real rows.
"""
import argparse, random, sys
from datetime import datetime, timedelta, timezone

import server

PREFIX = "demo-"
PEOPLE = [
    # name, sessions, style: tokens per session, cache quality, prompts per session
    ("demo-alice", 42, 1.0, 0.86, 9),    # long focused sessions, good cache reuse
    ("demo-bekzod", 28, 1.6, 0.42, 3),   # restarts constantly, pays for it
    ("demo-chen", 35, 0.6, 0.78, 14),    # lots of small asks
    ("demo-dilnoza", 12, 0.9, 0.66, 6),
]
PROJECTS = ["/Users/x/Projects/api", "/Users/x/Projects/web", "/Users/x/Projects/infra"]
MODELS = {"claude": ["claude-opus-5", "claude-sonnet-5"], "codex": ["gpt-5.6", "gpt-5.6-sol"]}
SAMPLE_PROMPTS = [
    "fix the failing test in payments/test_refund.py",
    "why is the checkout page rendering twice on mobile?",
    "add a retry with backoff around the webhook delivery",
    "review this diff for anything that breaks on empty input",
    "write the migration for the new invoices.status column",
    "the deploy is stuck, read the systemd logs and tell me what died",
    "make this query use the index instead of a full scan",
    "explain what this regex matches, I did not write it",
]


def seed(force=False):
    with server.db() as c:
        n = c.execute("SELECT COUNT(*) FROM sessions WHERE user NOT LIKE ?", (PREFIX + "%",)).fetchone()[0]
        if n and not force:
            print(f"{n} real sessions already here, not seeding (use --force)")
            return 0
    rnd = random.Random(7)  # same demo numbers every run
    now = datetime.now(timezone.utc)
    total = 0
    for user, count, weight, cache_quality, ppm in PEOPLE:
        sessions = []
        for i in range(count):
            tool = "claude" if rnd.random() < 0.65 else "codex"
            start = now - timedelta(days=rnd.uniform(0, 29), hours=rnd.uniform(0, 8))
            minutes = max(3, rnd.gauss(28, 18))
            prompts = max(1, int(rnd.gauss(ppm, ppm / 2)))
            fresh = int(prompts * rnd.gauss(9000, 2500) * weight)
            cached = int(fresh * cache_quality / max(0.05, 1 - cache_quality))
            out = int(prompts * rnd.gauss(1400, 500) * weight)
            texts = [{"at": (start + timedelta(minutes=j * 2)).isoformat(),
                      "chars": len(t := rnd.choice(SAMPLE_PROMPTS)), "text": t}
                     for j in range(min(prompts, 6))]
            sessions.append({
                "session_id": f"{PREFIX}{user}-{i}", "tool": tool,
                "project": rnd.choice(PROJECTS), "models": [rnd.choice(MODELS[tool])],
                "prompts": prompts, "assistant_turns": prompts * 2,
                "tool_calls": int(prompts * rnd.uniform(1.5, 9)),
                "input_tokens": max(0, fresh // 4), "output_tokens": max(0, out),
                "cache_read_tokens": max(0, cached), "cache_write_tokens": max(0, fresh),
                "reasoning_tokens": int(out * 0.3),
                "started_at": start.isoformat(),
                "ended_at": (start + timedelta(minutes=minutes)).isoformat(),
                "prompt_texts": texts,
            })
        server.ingest({"user": user, "machine": user.replace(PREFIX, "") + "-mbp",
                       "sessions": sessions})
        total += len(sessions)
    print(f"seeded {total} sample sessions across {len(PEOPLE)} demo users")
    return total


def clear():
    with server.db() as c:
        c.execute("DELETE FROM prompts WHERE user LIKE ?", (PREFIX + "%",))
        n = c.execute("DELETE FROM sessions WHERE user LIKE ?", (PREFIX + "%",)).rowcount
    print(f"removed {n} sample sessions")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()
    clear() if a.clear else seed(a.force)
