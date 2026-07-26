# Engineering log

Selected incidents from running this assistant as two real daily-driver installs
(`penny` and `jarvis`) since July 8, 2026. Every entry follows the same
discipline: the bug was found in production use, root-caused against the actual
database and logs (not just the chat transcript), fixed, and covered by a
regression test before the fix shipped. Personal content from the real users'
lists has been replaced with generic examples; the engineering detail is
unchanged.

---

## Jul 8 — Launch night: a truncated response silently discarded the first big message

**Symptom:** the very first large brain-dump message (eight-plus tasks in one
paragraph) got back a bare "Done." with *nothing* saved.

**Root cause:** `max_tokens=900` truncated the model mid-way through a batch of
parallel `capture_item` tool calls. The tool loop broke on
`stop_reason != "tool_use"` without executing any of the complete tool calls it
had already received, then emitted a fallback confirmation anyway.

**Fix:** raised the ceiling to 4096, made the loop execute every *complete*
tool block even when the response is truncated, and replaced the blind
fallback with action-aware ones ("Got it — X, Y, Z. All on the list." /
"That didn't save properly on my end — send it once more?") derived from what
actually ran. This incident seeded the project's core rule: **never confirm
what didn't happen** — which later grew into the empty-promise guard.

## Jul 9–10 — Three trust failures from the first field test

1. **The bot denied prior conversations existed.** The chat history sent to the
   model is a sliding window; once the first day scrolled out, "what did I tell
   you last night?" got "this is our first chat." Fix: the SQLite state (items +
   facts) is declared the permanent record in the prompt; "what did I tell you"
   is answered from state, never from the visible scroll.
2. **It argued with the owner about her own birthday.** Contradictory facts had
   accumulated, and the model defended the stale one. Fix: corrections-are-law —
   `remember_fact` gained `replaces_fact_id`, fact IDs are exposed in the state
   block, and the prompt states the owner is always right about her own life.
3. **Bare "Done." replies destroyed trust** (which done? saved what?). Fix:
   every confirmation must name what changed, enforced by the deterministic
   fallback path as well as the prompt.

## Jul 12 — The notification flood, and the first empty promise

**Symptom:** one owner received ~25 notifications in a single day and asked for
"two or three." The bot replied "I've turned off your evening digest and set
reminders to a gentler pace" — and had done **neither** (verified directly in
the settings table: no tool call ever ran).

**Root cause, flood:** nag chains had no concept of "this moment has passed."
Two items tied to an event that had already happened kept escalating every ~6
hours (7 and 8 nags each) because only a `max_nags=12` counter could stop a
chain. Separately, outbound pings were never logged to the conversation store,
so the model literally could not see how often it had pinged.

**Root cause, empty promise:** nothing verified that a claimed change had a
successful tool call behind it.

**Fixes (v1.5, shipped next day):**
- Scheduled reminders ("ping me at 3pm") split into their own table, separate
  from the overdue-chase nag chain, which now only starts after `due_at` passes
  and stops entirely 24h later (stale items fall to a gentle morning-digest
  section with clear buttons instead of nagging forever).
- A daily ping budget (`daily_ping_cap`, owner-adjustable by chat), same-tick
  bundling into one message, `MAX_NAGS` 12→5, a mute button, and every
  proactive send logged into the message history with a `[reminder]`/`[digest]`
  prefix so the model can see its own pings.
- The **empty-promise guard**: a deterministic check after every turn — if the
  reply claims a change ("turned off", "saved that", "on the calendar") and zero
  tool calls succeeded, one corrective round-trip forces real tool calls or an
  honest walk-back. Trips are counted in the database and surfaced in a report
  CLI, so this failure class became measurable instead of anecdotal.

## Jul 13 — The flood fix's own bug: a one-per-minute nag storm

Within hours of deploying v1.5, one item nagged 13+ times, a minute apart.
`due_nags()` couldn't distinguish "never nagged yet" from "nagging deliberately
stopped" — both looked like `next_remind_at IS NULL` — so every intentional
stop was re-selected as a fresh item on every minute tick. Fixed by checking
`remind_count > 0` to separate the two NULL states, plus a guarantee that the
give-up path always leaves `remind_count >= 1` even for an item that first
surfaces already 24h overdue. Regression test confirms a stopped chain stays
stopped across multiple subsequent ticks, and that a fresh overdue item still
fires its first nag (the fix must not overcorrect).

## Jul 14 — Silent outage: both bots alive, neither working

Both instances went unresponsive for ~6 hours: processes alive, launchd
`KeepAlive` never triggered (nothing crashed), scheduler jobs still firing, but
the Telegram long-poll stuck in a network-error loop (444+ `ReadError`/
`TimedOut` occurrences). `curl` to the Telegram API from the same host worked
the whole time — the wedged connection pool was local to the two long-running
processes. Nobody noticed until an owner happened to send a message.

**Fix (Jul 16):** `heartbeat.py`, an independent liveness watchdog running as
its *own* launchd job every 5 minutes — deliberately not inside either bot
process. Per instance it checks: does the PID exist; has the log shown any
activity in 15 minutes (catches a fully hung process); and has the log shown
5+ Telegram network errors in 10 minutes (catches this exact stuck-poller
signature, which an is-it-logging check would miss, since the scheduler kept
logging happily through the entire outage). It alerts via a macOS local
notification rather than Telegram — alerting through the channel that might be
the broken thing would defeat the purpose.

Building it surfaced a classic silent failure: the first version built its
AppleScript with Python's `!r` repr, which single-quotes strings; AppleScript
requires double quotes, so every notification failed silently
(`subprocess.run` without `check=True` swallowed the nonzero exit). Found only
by firing a real test notification and getting nothing. Fixed with proper
escaping and `check=True`, and the script-building extracted into a pure
function with unit coverage.

## Jul 15 — The caching that had never worked

Cost review found two back-to-back turns 11 seconds apart that each cost
$0.0078 — the second should have been nearly free with a warm prompt cache.

**Bug one:** the state block opened with a to-the-minute timestamp sitting
between the cached personality prefix and everything else. Prompt caching is
prefix-matched, so that one line invalidated the cache on every turn, no
matter how close together. Fixed by moving the timestamp into its own tiny
block *after* the state block's cache breakpoint (stable → volatile ordering).

**Bug two, found while verifying against the real API:** caching hadn't been
engaging *at all*, for any block, since launch. Haiku's minimum cacheable
prompt length turned out to be ~4,096 tokens (bisected empirically: no cache at
~3,900, engages at ~4,140+), and this bot's tools+personality prefix measured
3,716 tokens — just under the floor. Every cache-related change before this
date had been a no-op. The restructure bundled personality+state into one
breakpoint, pushing the real request to 5,051 tokens — verified live: 5,051
tokens written to cache on turn one, all 5,051 read back on turn two, with only
~350 tokens (timestamp + new message) billed fresh.

Lesson that stuck: **a cost optimization isn't real until you've watched the
usage numbers come back from the API.** A warm turn now costs ~$0.002; a
typical day runs a few cents.

## Jul 16 — The guard forced a false choice, and the model fabricated an action

An owner asked a plain diagnostic question ("why did you nudge me a minute
after the reminder?"). The model's accurate self-diagnosis tripped the
empty-promise guard, whose corrective instruction offered only two outcomes:
"make the tool call now" or "admit you can't." With no room for "nothing needs
fixing — you just asked a question," the model invented an `update_item` call
and silently moved a real due date a day out. Fixed the prompt to name the
third outcome explicitly (just explain, no tool call), fixed the underlying
double-nudge (a same-time reminder+nag now has a grace period), and widened the
guard's log line from 200 to 600 chars so the next incident is diagnosable from
the log alone. The item's date was restored after confirming with the owner.

Design lesson: **a guardrail that forces a binary choice will manufacture the
action it demands.** Every corrective path since has included a no-op exit.

## Jul 16 — Structural cleanup after six incident-driven patches

An external code review flagged accumulated debt; each claim was verified
against the code before acting (most held). Shipped the highest-leverage items:
`complete_item` made atomic (completing a recurring item and spawning its next
occurrence used to be two transactions — a crash between them silently ended
the series); tool results became structured `ToolResult(ok, message)` instead
of inferring success by string-prefix sniffing (which the guard's bookkeeping
depended on); three privately-named helpers that two other modules were already
importing got promoted to public names; and the digest's item-bucketing logic
was unified into one function after the Python filter and the model prompt's
English copy of the same rule drifted apart once. Deliberately deferred the
bigger refactors with reasons recorded — for a two-user personal tool, incident
response beats architecture polish.

## Jul 18 — Guarding the guard

An item's progress text read "0/100" all day while the owner reported progress
three times and got three confident confirmations. The guard *did* catch each
hollow claim and fired its corrective retry — but nothing checked whether the
retry itself made a real tool call, and in all three cases the retry produced a
second hollow claim that went straight through. Fixed: the claims check now
runs on the retry's own reply too; a second hollow claim is replaced with an
honest "That didn't actually save on my end — mind sending it again?" A mocked
two-hollow-claims-in-a-row test replays the incident; a companion test confirms
a retry that does make a real call is still trusted.

## Jul 19–21 — Five incidents on one feature → replace the design, not the patch

Numeric progress tracking ("N/100 done today") was represented as two
independently hand-edited free-text fields (`title` and `reminder_text`).
Five distinct production incidents followed in four days: hollow-claim
confirmations, an unrelated item wrongly completed in the same turn, the two
text fields drifting out of sync (the update landed on the field the scheduler
doesn't display), and finally a fabricated count ("Done — checked off 25/100"
after the owner said they'd done none). Each earlier fix had patched a symptom
of the same root design.

**Fix:** real `progress_current`/`progress_target` columns; a dedicated
`log_progress` tool as the *only* path that can change the count (enforced in
code — `update_item` silently ignores the field); reaching the target
auto-completes and respawns atomically; and `reminder_text` became a stable
template ("Pushups today: {current}/{target} done") filled from the columns at
ping time, so no stored text can drift. 19 new tests around the mechanism.

Lesson: **when the same feature produces incidents on consecutive days, stop
patching symptoms and change the data model so the failure is unrepresentable.**

## Jul 23 — Weekday arithmetic is not a language-model strength

"The appointment is actually Thursday, not Wednesday" got stored as a Friday
date, and a late-completed daily recurring item respawned with its reminder
already in the past (instant re-nag one minute after check-off). The respawn
now rolls forward by whole cycles until the reminder is genuinely in the
future, and the per-turn context gained a pre-computed table of upcoming dates
labeled by weekday — resolving "next Thursday" became a lookup instead of
mental math. When the one-week table still wasn't enough (a "next Friday"
mixup two days later), it was extended to 14 days, includes today, and is named
in the prompt as the *only* source for weekday↔date answers.

## Jul 25 — Full transcript review: the guard was crying wolf

A review of both bots' complete transcripts for the week (checked against the
items table, not just impressions) found 26 guard trips — and every sampled
trip was a *presentation* failure, not a data failure. The corrective retry was
doing the real tool call, but its prose apologized ("You're right — I didn't
actually capture that") about work that had succeeded seconds earlier, because
the model treated the injected correction as if the owner had complained.
Prompt instructions not to do this had already failed. Fixed deterministically:
the retry path snapshots what actually succeeded before and after, and if the
retry's reply still contains self-correcting prose about work that succeeded,
the apology is discarded and replaced with the deterministic confirmation.

The same review shipped 15 more fixes, including: past-dated captures rejected
at the tool boundary ("remind me in an hour" once got saved three hours in the
past and fired instantly, then nudged every 30 minutes — 6 pings in 2 hours);
a roll-forward pass so a missed day can't strand a recurring item
permanently-overdue; a partial-fulfillment guard (a turn that does three real
things and fabricates a fourth used to sail past the zero-tool-calls check —
now claims are checked by *kind*, deterministically, at no added latency); and
a rule that a clarifying question may never swallow a multi-part message — the
unambiguous parts get saved first, then the question covers only the genuine
remainder.

---

**Where it stands:** 222 unit tests (zero API cost) plus a 35-check live suite
against the real model, budget-capped per run. Reliability incidents are
counted in the database, not remembered in anyone's head. The product runs
Haiku-only by design, at a few cents per day per user — the interesting
engineering was never about a bigger model; it was about making a small one
impossible to disbelieve.
