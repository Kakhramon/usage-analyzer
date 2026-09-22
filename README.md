# usage-analyzer

One dashboard for a shared Claude Max or Codex plan: who spent the tokens, and
whether their sessions and prompts were worth it.

Installs as a plugin for **Claude Code** and **Codex**. Pure stdlib Python 3 — no
dependencies, no npm, no service to sign up for. You host the dashboard.

## Install

Claude Code:

```
/plugin marketplace add kakhramon/usage-analyzer
/plugin install usage-analyzer
```

Codex: add the same repository as a plugin source, then install `usage-analyzer`.

Then, inside either tool:

```
/usage-analyzer:setup alice https://usage.example.com SHARED_SECRET
```

That writes `~/.usage-analyzer.json` and does a first sync. After that a `SessionEnd`
hook syncs automatically whenever a session ends — no command to remember.

## Server (one person runs this, once)

```bash
python3 scripts/server.py --port 8080 --token SHARED_SECRET
```

The dashboard is the root URL: `http://your-host:8080/`. Data lives in `usage.db`
beside `server.py`; override with `USAGE_DB=/path/usage.db`. Put it behind a reverse
proxy for TLS. Without `--token` anyone can post fake usage, so set one and give the
same secret to everyone during setup.

## What leaves the machine

Counts only: session id, token totals, prompt and tool-call counts, timestamps, model
names, and the project directory path. No prompt text, no file contents, no code.

Logs read: `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl`. Only
files whose size or mtime changed since the last run are re-read, so a routine sync
costs a `stat()` per file. Sessions are keyed by id and upserted, so a re-sync never
double-counts.

## Skills

| | |
|---|---|
| `setup` | set your name and the dashboard URL, first sync |
| `usage` | ask for your own or the team's numbers in chat, without opening the browser |

## Dashboard columns

- **total tokens / share** — who is consuming the plan.
- **tokens/prompt** — high means a huge context per ask; the usual cause is long
  sessions that were never `/clear`ed.
- **cache hit** — cached input over all input. Below ~40% means context is being
  rebuilt from scratch every turn, which is the expensive way to work.
- **tools/prompt** — how much work the agent does per instruction.
- **prompts/session** — very low means throwaway sessions, very high means the
  session should have been split.

Subagent (sidechain) traffic is folded into its parent session, not counted separately.

## Manual use, without the plugin

```bash
python3 scripts/collect.py init --name alice --url https://usage.example.com --token SECRET
python3 scripts/collect.py            # sync now
python3 scripts/collect.py --dry-run  # show what would be sent
```

Or from cron, if you do not want the hook:

```
*/15 * * * * /usr/bin/python3 /path/to/scripts/collect.py --quiet
```

## Test

```bash
python3 scripts/test_usage.py
```

MIT.
