---
name: setup
description: Configure usage-analyzer with your display name and the team dashboard URL, then do the first sync. Use when the user installs the plugin, says their usage is not showing up, wants to change the name they appear under, or wants to point at a different dashboard.
arguments: "[name] [dashboard-url] [token]"
allowed-tools: Bash(python3:*), Bash(cat:*)
disable-model-invocation: true
---

Configure this machine to report usage. Arguments, possibly empty: `$ARGUMENTS`

1. Check current config: `cat ~/.usage-analyzer.json` (missing file means not set up yet).
2. You need three things. Take them from the arguments, else from what the user
   already said, else ask for the missing ones in a single question:
   - **name** — how they appear on the dashboard (`alice`, `Bekzod`). Short, no spaces.
   - **url** — the dashboard URL the team already runs, e.g. `https://usage.example.com`.
     If nobody runs one yet, say so and point at the "Server" section of the plugin README;
     do not start a server on their laptop unless they ask for it.
   - **token** — the shared secret that dashboard was started with. Optional only if
     the server runs without `--token`.
3. Run:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/collect.py" init --name NAME --url URL --token TOKEN
   ```

4. Do the first sync and report the result. It reads every local session log once,
   so allow a minute on an old machine:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/collect.py"
   ```

5. Tell them: from now on every session end syncs automatically, and the dashboard
   is at the URL they gave. Mention once that only counts are sent — session ids,
   token totals, timestamps, model names, project paths — never prompt or file content.

If step 4 fails with a connection error, the dashboard URL is wrong or the server is
down; with `bad token`, the secret does not match the server's `--token`.
