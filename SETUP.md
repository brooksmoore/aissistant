# Setup

One codebase, many independent assistants. Each **instance** (e.g. `penny`, `jarvis`) lives
under `instances/<name>/` with its own Telegram bot, Anthropic API key, SQLite database,
Google tokens, backups, and log. Core setup is ~15 minutes; the optional Google part is ~20
more and can be done any time later.

Requirements: a Mac that stays on (the bot runs via launchd), Python 3.11+, a Telegram
account, and an Anthropic API key from console.anthropic.com.

## Step 1 — Install Telegram (2 min, the owner's phone)

App Store → **Telegram** → sign up. Free.

## Step 2 — Create the bot (3 min)

1. In Telegram, search **@BotFather** (blue checkmark) and open it.
2. Send `/newbot`.
3. Display name → e.g. `Penny`.
4. Username → something like `my_assistant_bot` (must end in `bot`, must be unused).
5. BotFather replies with a **token** like `7712345678:AAF...xyz`. Copy it.

## Step 3 — Install and configure (3 min, the Mac)

```bash
git clone https://github.com/brooksmoore/aissistant.git ~/aissistant
cd ~/aissistant
bash setup.sh <name>            # e.g. bash setup.sh penny — creates venv + instances/<name>/.env
open -e instances/<name>/.env
```

Fill in:

- `TELEGRAM_TOKEN` — from step 2
- `ANTHROPIC_API_KEY` — a dedicated key per instance keeps spend attribution clean
- `PAIRING_CODE` — a secret word; the owner texts it once to claim the bot (strangers are ignored)
- `TIMEZONE` — leave blank to auto-detect from the Mac
- Optionally: `ASSISTANT_NAME`, digest times, owner pronouns (`OWNER_PRONOUN_*`), and
  `BRAIN_MODEL` (`claude-haiku-4-5-20251001` is the all-Haiku maximum-savings mode both
  reference installs run; the default routes hard turns to Sonnet)

Note: launchd cannot execute from `~/Desktop` or other TCC-protected folders — keep the
repo in your home directory (e.g. `~/aissistant`).

## Step 4 — First run + pairing (2 min)

```bash
bash run.sh <name>
```

On the phone: find the bot's username from step 2, tap **Start**, text it the pairing code.
A welcome message means it's live — start brain-dumping. Only that chat is served from then on.

## Step 5 — Make it permanent (1 min)

Ctrl+C the foreground run, then:

```bash
bash install_autostart.sh <name>
bash install_heartbeat.sh        # optional: liveness watchdog, alerts if a bot silently hangs
```

Runs 24/7, survives reboots, restarts on crash. Logs: `tail -f instances/<name>/assistant.log`

## Step 6 — Google Calendar + Gmail (optional, ~20 min, one time)

Unlocks calendar editing, the calendar section of the morning digest, and inbox triage
(read-only; it can never send, delete, or modify email).

1. **console.cloud.google.com** — sign in with the owner's personal Gmail.
2. Project dropdown → **New Project** → name it → Create → select it.
3. Enable **Google Calendar API** and **Gmail API** (search bar).
4. **OAuth consent screen**: app name, owner's email, audience **External** → Create.
   Under Audience → Test users → add the owner's Gmail address.
   Then **Publish app** (otherwise tokens expire every 7 days).
5. **Credentials** → Create credentials → **OAuth client ID** → Desktop app → Download JSON.
6. Save it as `instances/<name>/google_credentials.json`.
7. Then:
   ```bash
   AISSISTANT_INSTANCE=<name> ./venv/bin/python setup_google.py
   ```
   A browser opens; the owner logs in and clicks Allow ("unverified app" warning is expected
   for a personal app). Restart the bot: `bash install_autostart.sh <name>`.

## Costs

- Telegram, hosting (your Mac), reminders, digests, buttons: free — none of them call the API.
- Claude API: prompt caching plus a two-tier budget breaker. Past `DAILY_BUDGET_USD`
  (default $0.20/day) everything falls back to Haiku; past `HARD_CAP_USD` (default $0.25/day)
  API calls stop until midnight while reminders, buttons, and plain digests keep working.
- Measured in real use: a warm conversational turn is ~$0.002; a typical day runs a few
  cents. `/stats` in chat shows today's estimated spend; `AISSISTANT_INSTANCE=<name>
  ./venv/bin/python report.py 7` shows a week.

## Tests

```bash
bash run_tests.sh          # 222 unit tests, zero API cost, ~3 seconds
bash run_tests.sh --live   # 35 checks against the real model, budget-capped, ~$0.10
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't reply | `tail -20 instances/<name>/assistant.log` — usually a bad token or no internet |
| "TELEGRAM_TOKEN is missing" | You edited `.env.example` instead of `instances/<name>/.env` |
| Reminders not arriving | Is the Mac awake? The launchd job wraps the bot in `caffeinate`, but check System Settings sleep behavior, and that the chat isn't muted in Telegram |
| launchd exit code 78 | The repo is in a TCC-protected folder (Desktop/Documents) — move it to `~/` |
| Google stopped working | Token expired or revoked — re-run step 6.7. Publishing the OAuth app (step 6.4) prevents the 7-day expiry |
| Reset pairing | `sqlite3 instances/<name>/assistant.db "DELETE FROM settings WHERE key='owner_chat_id';"` then text the code again |
| Start over | Stop the bot, delete `instances/<name>/assistant.db`, restart |

## Where the data lives

Everything is in `instances/<name>/` — the SQLite database (list, facts, chat history,
spend tallies), nightly backups (30-day retention), Google tokens, and the log. The whole
directory is gitignored. Nothing leaves the machine except calls to Anthropic (the brain),
Telegram (delivery), and Google (if enabled).
