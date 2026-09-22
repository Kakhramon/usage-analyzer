---
name: usage
description: Show usage from the team dashboard - who spent the most tokens, how efficiently people are working, or just the user's own numbers. Use when the user asks about their usage, the team's usage, who is burning the shared plan, or how well they are using their sessions.
arguments: "[days] [user]"
allowed-tools: Bash(python3:*), Bash(curl:*), Bash(cat:*)
disable-model-invocation: true
---

Report usage from the team dashboard. Arguments, possibly empty: `$ARGUMENTS`

1. Read `~/.usage-analyzer.json` for `url` and the user's own `name`. If that file is
   missing, tell them to run the `setup` skill first and stop.
2. Fetch the window they asked for, defaulting to 30 days:

   ```bash
   python3 - <<'PY'
   import json, urllib.request
   from datetime import date, timedelta
   from pathlib import Path
   cfg = json.loads((Path.home() / ".usage-analyzer.json").read_text())
   days = 30  # replace with what the user asked for
   since = (date.today() - timedelta(days=days)).isoformat()
   with urllib.request.urlopen(cfg["url"].rstrip("/") + f"/api/stats?since={since}", timeout=30) as r:
       print(json.dumps(json.load(r)["by_user"], indent=2))
   PY
   ```

3. Answer in a short table, biggest consumer first. One row per person, merging their
   `claude` and `codex` rows unless the user asked to split by tool. Derive:
   - **total tokens** = input + output + cache read + cache write
   - **tokens/prompt** = total tokens / prompts
   - **cache hit** = cache read / (cache read + input + cache write), as a percent
   - **prompts/session**, **tools/prompt**
4. Add at most two lines of reading, and only where the numbers support it:
   cache hit under 40% means context is rebuilt every turn, usually a session that
   should have been `/clear`ed. Very high tokens/prompt with few tool calls means
   a big context is being carried for little work.
5. End with the dashboard URL, since it has the sortable version.

Do not guess numbers for someone whose row is absent — they have simply not synced.
