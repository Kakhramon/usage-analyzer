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
python3 scripts/server.py --port 8080 --token SHARED_SECRET --admin-token ADMIN_SECRET
```

Try it before anyone installs anything — `--demo` seeds four sample users with
sessions and prompts so the dashboard has something to show:

```bash
python3 scripts/server.py --port 8080 --demo
python3 scripts/seed_demo.py --clear   # drop the sample users later
```

The dashboard is the root URL: `http://your-host:8080/`. Data lives in `usage.db`
beside `server.py`; override with `USAGE_DB=/path/usage.db`. Put it behind a reverse
proxy for TLS. Without `--token` anyone can post fake usage, so set one and give the
same secret to everyone during setup.

## Settings — change anything after deployment

The **Settings** panel on the dashboard (needs `--admin-token`) writes to the server,
and every collector reads it on its next sync. Nobody re-installs anything.

| setting | what it does |
|---|---|
| `team_name` | the title on the dashboard |
| `retention_days` | delete sessions and prompts older than this; `0` keeps everything |

## What leaves the machine

Session counts — token totals, prompt and tool-call counts, timestamps, model names,
the project directory path, the machine name — and the full text of every prompt the
person typed. No file contents, no code, no assistant replies.

Credentials are stripped before anything is sent: API keys, `ghp_`/`xox` tokens, JWTs,
AWS keys, `password:`/`token=` pairs and private key blocks all become `[redacted]`,
along with the `<system-reminder>` blocks the tools inject. Everyone on the shared
plan can read everyone's prompts on the dashboard, so put it behind your VPN or
basic auth, and tell the team it is on.

Logs read: `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl`. Only
files whose size or mtime changed since the last run are re-read, so a routine sync
costs a `stat()` per file. Sessions are keyed by id and upserted, so a re-sync never
double-counts.

## Skills

| | |
|---|---|
| `setup` | set your name and the dashboard URL, first sync |
| `usage` | ask for your own or the team's numbers in chat, without opening the browser |

## Dashboard

Three levels, click through:

1. **Everyone** — one row per person, sorted by tokens, with a plain-language note
   when someone's numbers show an obvious problem. Filter by date range (presets or
   two date pickers) and by person; both apply to every level below.
2. **A person** — their projects, then every session with its own token and cache
   numbers.
3. **A session** — every prompt from it, in order, with timestamps.

People are stored in a `users` table as they sync — first seen, last seen, last sync
and machine name — so the person filter lists everyone on the plan even in a window
where they did nothing.

### Columns

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
