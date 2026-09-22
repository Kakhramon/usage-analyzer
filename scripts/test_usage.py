#!/usr/bin/env python3
"""Self-check: parsers + ingest + stats. Run: python3 test_usage.py"""
import json, os, sqlite3, tempfile, sys
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["USAGE_DB"] = str(tmp / "t.db")
sys.path.insert(0, str(Path(__file__).parent))
import collect, server

claude = tmp / "abc123.jsonl"
claude.write_text("\n".join(json.dumps(d) for d in [
    {"type": "user", "timestamp": "2026-09-01T10:00:00Z", "cwd": "/p",
     "message": {"content": "hello"}},
    {"type": "user", "timestamp": "2026-09-01T10:00:05Z",
     "message": {"content": [{"type": "tool_result", "content": "x"}]}},  # not a prompt
    {"type": "assistant", "timestamp": "2026-09-01T10:30:00Z", "message": {
        "model": "claude-opus-5", "content": [{"type": "tool_use", "name": "Bash"}],
        "usage": {"input_tokens": 10, "output_tokens": 20,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5,
                  "output_tokens_details": {"thinking_tokens": 7}}}},
    {"type": "assistant", "isSidechain": True, "timestamp": "2026-09-01T11:00:00Z",
     "message": {"usage": {"output_tokens": 999}}},  # subagent, ignored
]))
c = collect.parse_claude(claude)
assert c["prompts"] == 1, c["prompts"]
assert c["tool_calls"] == 1 and c["assistant_turns"] == 1
assert c["output_tokens"] == 20 and c["cache_read_tokens"] == 100
assert c["reasoning_tokens"] == 7 and c["models"] == {"claude-opus-5"}
assert [t["text"] for t in c["prompt_texts"]] == ["hello"]

codex = tmp / "rollout-x.jsonl"
codex.write_text("\n".join(json.dumps(d) for d in [
    {"type": "session_meta", "timestamp": "2026-09-02T08:00:00Z",
     "payload": {"session_id": "sess-1", "cwd": "/q"}},
    {"type": "turn_context", "timestamp": "2026-09-02T08:00:01Z", "payload": {"model": "gpt-5.6"}},
    {"type": "response_item", "timestamp": "2026-09-02T08:00:02Z",
     "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text",
                              "text": "deploy it, my key is sk-abcdefghijklmnopqrst"}]}},
    {"type": "response_item", "timestamp": "2026-09-02T08:00:03Z",
     "payload": {"type": "function_call"}},
    {"type": "event_msg", "timestamp": "2026-09-02T08:10:00Z", "payload": {
        "type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 500, "cached_input_tokens": 400, "cache_write_input_tokens": 10,
            "output_tokens": 60, "reasoning_output_tokens": 30}}}},
    {"type": "event_msg", "timestamp": "2026-09-02T08:05:00Z", "payload": {  # older, smaller
        "type": "token_count", "info": {"total_token_usage": {"input_tokens": 1}}}},
]))
x = collect.parse_codex(codex)
assert x["session_id"] == "sess-1" and x["prompts"] == 1 and x["tool_calls"] == 1
assert x["input_tokens"] == 100, x["input_tokens"]   # 500 total minus 400 cached
assert x["cache_read_tokens"] == 400 and x["output_tokens"] == 60

sess = dict(x, models=["gpt-5.6"], started_at="2026-09-02T08:00:00+00:00",
            ended_at="2026-09-02T08:10:00+00:00")
assert server.ingest({"user": "alice", "sessions": [sess]})["stored"] == 1
assert server.ingest({"user": "alice", "sessions": [sess]})["stored"] == 1  # upsert, no dupes
s = server.stats("2026-01-01")
assert len(s["by_user"]) == 1 and s["by_user"][0]["sessions"] == 1
assert s["by_user"][0]["total_tokens"] == 570, s["by_user"][0]["total_tokens"]
assert abs(s["by_user"][0]["active_minutes"] - 10) < 0.5

for bad in [{"user": "", "sessions": []}, {"user": "a", "sessions": [{"tool": "codex"}]}]:
    try:
        server.ingest(bad); raise SystemExit("bad payload accepted: %r" % bad)
    except ValueError:
        pass

# prompt capture: secrets never leave, noise never arrives
assert x["prompt_texts"][0]["text"] == "deploy it, my key is [redacted]", x["prompt_texts"]
assert collect.clean_prompt("real ask<system-reminder>noise</system-reminder>") == "real ask"
assert "[redacted]" in collect.clean_prompt("export AWS=AKIA1234567890ABCDEF")
assert collect.clean_prompt("password: hunter2hunter2") == "[redacted]"

# capture mode gates what scan() emits, and is part of the change fingerprint
collect.CLAUDE_DIR, collect.CODEX_DIR = tmp, tmp / "none"
got = {s["session_id"]: s for _, _, s in collect.scan({}, "off")}
assert got["abc123"]["prompt_texts"] == []
got = {s["session_id"]: s for _, _, s in collect.scan({}, "truncated", 3)}
assert got["abc123"]["prompt_texts"][0]["text"] == "hel"
state = {}
fps = [fp for _, fp, _ in collect.scan(state, "off")]
state.update({p: f for (p, f, _) in collect.scan({}, "off")})
assert not list(collect.scan(state, "off")), "unchanged files re-sent"
assert list(collect.scan(state, "full")), "capture change must re-send"

# prompts are stored and readable per session
sess2 = dict(sess, session_id="sess-2", prompt_texts=[
    {"at": "2026-09-02T08:00:02+00:00", "chars": 5, "text": "hello"}])
r = server.ingest({"user": "alice", "sessions": [sess2]})
assert r["prompts_stored"] == 1, r
got = server.session_prompts("alice", "sess-2")["prompts"]
assert len(got) == 1 and got[0]["text"] == "hello"
assert server.ingest({"user": "alice", "sessions": [sess2]})["prompts_stored"] == 1  # idempotent
assert len(server.session_prompts("alice", "sess-2")["prompts"]) == 1

# user drill-down
d = server.user_detail("alice", "2026-01-01")
assert len(d["sessions"]) == 2 and len(d["projects"]) == 1
assert [s["saved_prompts"] for s in d["sessions"] if s["session_id"] == "sess-2"] == [1]

# settings round-trip and validation
assert server.get_settings()["capture_prompts"] == "off"
assert server.set_settings({"capture_prompts": "full", "retention_days": 30})["retention_days"] == 30
assert server.get_settings()["capture_prompts"] == "full"
assert server.stats("2026-01-01")["settings"]["retention_days"] == 30
for bad in [{"capture_prompts": "sometimes"}, {"nope": 1}, {"retention_days": -5}]:
    try:
        server.set_settings(bad); raise SystemExit("bad setting accepted: %r" % bad)
    except ValueError:
        pass

# retention deletes old rows, and their prompts with them
server.set_settings({"retention_days": 1})
server.ingest({"user": "alice", "sessions": [dict(sess2, session_id="sess-3")]})
assert not [s for s in server.user_detail("alice", "0000")["sessions"]], "old rows survived purge"
assert not server.session_prompts("alice", "sess-2")["prompts"], "orphan prompts survived purge"
server.set_settings({"retention_days": 0})

# demo seed fills an empty dashboard
import seed_demo
assert seed_demo.seed(force=True) > 0
assert any(r["user"].startswith("demo-") for r in server.stats("0000")["by_user"])
seed_demo.clear()
assert not any(r["user"].startswith("demo-") for r in server.stats("0000")["by_user"])

print("ok")
