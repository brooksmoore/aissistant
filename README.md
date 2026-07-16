# aissistant — shippable personal AI assistants

One codebase, many independent assistants. Each **instance** (e.g. `penny`, `jarvis`) has its
own Telegram bot, Anthropic API key, database/memories, Google account, backups, and log under
`instances/<name>/` — completely separate brains that all improve together when the code improves.

**Add a new instance in ~5 minutes:** create a bot via @BotFather + a fresh Anthropic key, fill
them into `instances/<name>/.env` (copy `.env.example`), then `bash install_autostart.sh <name>`
and text the bot your pairing code. Google (calendar/inbox) per instance:
`AISSISTANT_INSTANCE=<name> ./venv/bin/python setup_google.py` (README step 6 for the one-time
Google Cloud part, done in THAT person's Google account). Owner report per instance:
`AISSISTANT_INSTANCE=<name> ./venv/bin/python report.py 7`. Tests: `bash run_tests.sh [--live]`.

---

# Penny — the original instance 💛

A Telegram bot with a Claude brain that lives on Brooks's Mac. It captures everything she
says (via iPhone mic dictation), holds a prioritized master list with satisfying ✓ check-off
buttons, nags intelligently until things are done, sends morning/evening digests, edits her
Google Calendar, and watches her personal Gmail inbox for things that need her.

**Total setup time: ~15 minutes for the core, +20 minutes for the Google (calendar/email) part.**
The Google part is optional and can be done any time later — everything else works without it.

---

## Step 1 — She installs Telegram (2 min, her phone)

App Store → **Telegram** → sign up with her phone number. Free.

## Step 2 — Create her bot (3 min, either phone or Mac)

1. In Telegram, search for **@BotFather** (blue checkmark) and open it.
2. Send it: `/newbot`
3. It asks for a display name → e.g. `Penny 💛`
4. It asks for a username → something like `my_assistant_bot` (must end in `bot`, must be unused).
5. BotFather replies with a **token** that looks like `7712345678:AAF...xyz`. **Copy it.**

## Step 3 — Configure (3 min, the Mac)

```bash
cd ~/penny
bash setup.sh          # installs everything into a private venv
open -e .env           # opens the config file
```

In `.env` fill in:
- `TELEGRAM_TOKEN` — from step 2
- `ANTHROPIC_API_KEY` — your key from console.anthropic.com (the one in your ~/.zshrc works)
- `PAIRING_CODE` — pick a secret word; she'll text it to the bot once to claim it (keeps strangers out)
- `TIMEZONE` — set correctly if not Eastern (e.g. `America/Chicago`, `America/Los_Angeles`)
- Optionally rename `ASSISTANT_NAME` and adjust digest times.

## Step 4 — First run + pairing (2 min)

```bash
bash run.sh
```

On her phone: search Telegram for the bot's username from step 2, tap **Start**, and text it
the pairing code word. She gets a welcome message and she's live — she can start brain-dumping
immediately. **Only her chat works from then on; everyone else is ignored.**

## Step 5 — Make it permanent (1 min)

Ctrl+C the foreground run, then:

```bash
bash install_autostart.sh
```

Now it runs 24/7, survives reboots, restarts itself if it crashes.
Logs: `tail -f ~/penny/penny.log`

## Step 6 — Google Calendar + Gmail (optional, ~20 min, one time)

This unlocks: bot edits her calendar, morning digest shows her day, inbox watcher pings her
about emails that actually need her (and tracks Amazon deliveries). Skip it for now if you
want — nothing else depends on it.

1. Go to **console.cloud.google.com** — sign in with **her personal Gmail**.
2. Top bar → project dropdown → **New Project** → name it `penny` → Create (then select it).
3. Search bar: **"Google Calendar API"** → Enable. Then **"Gmail API"** → Enable.
4. Search bar: **"OAuth consent screen"** → get started/configure:
   - App name `Penny`, her email for both email fields, audience **External**, Create.
   - Under **Audience** → Test users → **+ Add users** → add her Gmail address.
5. Search bar: **"Credentials"** → **+ Create credentials** → **OAuth client ID** →
   Application type **Desktop app** → Create → **Download JSON**.
6. Rename the downloaded file to `google_credentials.json` and put it in `~/penny/`.
7. Then:
   ```bash
   cd ~/penny
   ./venv/bin/python setup_google.py
   ```
   A browser opens → **she** logs in and clicks Allow (it will warn "unverified app" — that's
   expected for personal apps; click Continue). Then restart the bot:
   ```bash
   bash install_autostart.sh
   ```

Permissions used: calendar read/write, **Gmail read-only** (it can never send, delete, or
modify her email).

---

## What it costs

- Telegram: free. Hosting: free (your Mac). Reminders/digests/buttons: free (no API calls).
- Claude API — engineered down hard, three layers:
  1. **Smart routing**: everyday turns (capture, check-off, "what's on my list") run on
     Haiku; Sonnet fires only for decisions, planning, drafting, or photos.
  2. **Prompt caching**: the big static prompt is cached (~90% off input tokens during a
     back-and-forth conversation).
  3. **Daily breaker**: past `DAILY_BUDGET_USD` (default $0.20) everything falls back to
     Haiku — she never loses service, it just gets cheaper. Past `HARD_CAP_USD` (default
     $0.25) API calls stop entirely until midnight; reminders, buttons, and plain-text
     digests keep working free.
  - Want all-Haiku (max savings, slightly weaker "help me decide" moments)? One line in
    `.env`: `BRAIN_MODEL=claude-haiku-4-5-20251001`
- Expected: **~$0.02–0.05/day** with normal use (measured ~quarter to half a cent per
  message), worst case pinned near the $0.25 cap.
  Check anytime by texting the bot `/stats` (shows today's estimated spend).
- Heads-up: this shares your `ANTHROPIC_API_KEY` spend with the trading bots — give it its
  own key if you want clean attribution.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't reply | `tail -20 penny.log` — usually a bad token or no internet |
| "TELEGRAM_TOKEN is missing" | You edited `.env.example` instead of `.env` |
| Reminders not arriving | Is the Mac awake? System Settings → prevent sleep (or Amphetamine app). Check she didn't mute the chat in Telegram |
| Google stuff stopped working | Token expires after ~7 days while the OAuth app is in "testing" mode. Either re-run `setup_google.py`, or in Cloud Console → OAuth consent screen → **Publish app** (fine for personal use) which makes tokens permanent |
| Want to reset the pairing | `sqlite3 penny.db "DELETE FROM settings WHERE key='owner_chat_id';"` then she texts the code again |
| Wipe everything and start over | Stop the bot, delete `penny.db`, restart |

## Where her data lives

Everything is in `penny.db` (SQLite) in this folder — her list, memory of her life, chat
history. Nothing goes anywhere except Anthropic's API (for the brain) and Google's API
(calendar/email, if enabled). Back it up occasionally: `cp penny.db penny.backup.db`.

## Files

- `bot.py` — main app (Telegram wiring, buttons, jobs)
- `brain.py` — Claude personality + tools (capture, complete, remember, calendar)
- `memory.py` — SQLite storage
- `scheduler.py` — escalating reminders + morning/evening digests
- `gmail_watch.py` — inbox triage (Haiku classifies; only real stuff pings her)
- `gcal.py` — Google Calendar read/write
- `FOR_HER.md` — the one-pager to show her
